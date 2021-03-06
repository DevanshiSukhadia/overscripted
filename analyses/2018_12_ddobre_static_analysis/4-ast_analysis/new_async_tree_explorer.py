#!/usr/bin/env python3

import configparser
import esprima
import glob
import json
import multiprocessing
import pandas as pd
import sys

from os import path
from tqdm import tqdm

################################################################################

config = configparser.ConfigParser()
config.read("config.ini")

# Top directory for all data and resource files
DATATOP = config["DEFAULT"]["datatop"]

# Stage 1 output:
#   directory for url:filename dictionary files (PARQUET!)
URL_FILENAME_DICT = path.join(DATATOP, config["DEFAULT"]["url_filename_dict"])

# Stage 2 output:
#   path to downloaded javascript files
JS_SOURCE_FILES = path.join(DATATOP, config["DEFAULT"]["js_source_files"])

# Stage 3 output:
#   specify the list of symbols you want to search for in the AST scan
SYM_LIST = config["DEFAULT"]["sym_list"]

# Output directory
OUTPUT_DIR = path.join(DATATOP, config["DEFAULT"]["output_dir"])

OUTPUT_FILE = config["DEFAULT"]["output_file"]
OUTPUT_FAIL = config["DEFAULT"]["output_fail"]

# Number of workers:
WORKERS = multiprocessing.cpu_count()

# Number of files per queue batch
BATCH_SIZE = config["DEFAULT"].getint("batch_size")

################################################################################
class SymbolNode:

    def __init__(self, depth, width, parent_depth, parent_width):
        self._depth = depth
        self._width = width
        self._parent_depth = parent_depth
        self._parent_width = parent_width

    def setDepthWidth(depth, width):
        self._depth = depth
        self._width = width

class CustomEncoder(json.JSONEncoder):
    def default(self, obj):

        # Symbol node class
        if isinstance(obj, SymbolNode):
            return{ "depth" : obj._depth,
                    "width" : obj._width,
                    "parent_depth": obj._parent_depth,
                    "parent_width": obj._parent_width}

        return json.JSONEncoder.default(self, obj)

################################################################################
class Element:

    ## Define keys you want to skip over
    BLACKLISTEDKEYS = ['parent']

    ## Constructor
    def __init__(self, esprima_ast):
        self._ast = esprima_ast         # Assign member var AST
        self._visitors = []             # Init empty visitor array


    ## Add a new visitor to execute (will be executed at each node)
    def accept(self, visitor):
        self._visitors.append(visitor)


    ## (private) Step through the node's queue of potential nodes to visit
    def _step(self, node, queue, depth, width):
        before = len(queue)

        for key in node.keys():         # Enumerate keys for possible children
            if key in self.BLACKLISTEDKEYS:
                continue                # Ignore node if it is blacklisted

            child = getattr(node, key)  # Assign child = node.key

            # if the child exists && the child has an attribute 'type'
            if child and hasattr(child, 'type') == True:
                child.parent = node     # Assign this node as child's parent
                child.parent_depth = depth
                child.parent_width = width
                queue.append(child)     # Append the child in this node's queue

            # if there is a list of children
            if isinstance(child, list):
                for item in child:      # Iterate through them and do the same
                                        #   as above
                    if hasattr(item, 'type') == True:
                        item.parent = node
                        item.parent_depth = depth
                        item.parent_width = width
                        queue.append(item)

        return len(queue) - before     # Return whether any children were pushed

    ## Walk through this AST
    def walk(self, api_symbols, filename):
        queue = [self._ast]             # Add the imported AST to the queue

        # Initialize these entries
        for node in queue:
            node.parent_depth = 0
            node.parent_width = 0

        # Depth and width counting
        depth                   = 0     # what level of the tree we are in
        width                   = 0     # how far from first node on this level we are
        this_depth_num_nodes    = 1     # how many nodes in this level are left
        next_depth_num_nodes    = 0     # how many nodes in the next level
        node_counter            = 0     # how many total nodes have been visited
        this_depth_count        = 0     # how many nodes are on this level (tot)

        # storage for the data
        extended_symbol_counter = {}
        symbol_counter          = {key: 0 for key in api_symbols}
        node_dict               = {key: [] for key in api_symbols}

        extended_symbol_counter['script_url_filename'] = filename

        while len(queue) > 0:           # While stuff in the queue
            node = queue.pop(0)          # Pop stuff off of the FRONT (0)
            this_depth_num_nodes -= 1
            node_counter += 1
            width = node_counter - this_depth_count - 1

            for v in self._visitors:    # Run visitor instances here
                result = v.visit(node, api_symbols)
                if result:
                    if result not in extended_symbol_counter.keys():
                        extended_symbol_counter[result] = 1;
                    else:
                        extended_symbol_counter[result] += 1;

                    #MemberExpression
                    if 'MemberExpression' == node.type:
                        tmp = node.property.name

                    # CallExpression
                    if 'CallExpression' == node.type:
                        tmp = node.callee.name

                    symbol_counter[tmp] += 1 # increment counter
                    this_node = SymbolNode(depth, width, node.parent_depth, node.parent_width)
                    node_dict[tmp].append(this_node)
                    break


            # If node is an instance of "esprima node", step through the node
            #   Returns how many children have been added to the queue
            if isinstance(node, esprima.nodes.Node):

                # Feed the nodes that will be labeled as children the current
                #   depth and width
                next_depth_num_nodes += self._step(node, queue, depth, width)


            # Once this tree depth has been walked, update with the existing
            #   "next" set and reset the next set to 0. Increment depth by 1,
            #   and keep a tally on how many nodes have been counted up until
            #   this depth.
            if this_depth_num_nodes == 0:
                this_depth_num_nodes = next_depth_num_nodes # update current list
                next_depth_num_nodes = 0                    # reset this list
                this_depth_count = node_counter             #
                depth += 1

        return symbol_counter, extended_symbol_counter, node_dict


################################################################################
"""
Executes specified code given that an input node matches the property name of
    this node.

Attributes:
    _property_name: the name of the property required to execute the handler
    _node_handler:  code to execute if _property_name matches
    visit(node):    checks if input node's property matches this nodes; if yes,
                        executes the code passed into _node_handler, passing the
                        input node as an argument
"""
class MatchPropertyVisitor:

    ## Constructor
    def __init__(self, property_name):
        self._property_name = property_name # userAgent, getContext, etc

    ##################################################
    def _recursive_check_objects(self, node, api_symbols):
        if node.object:
            return self._recurrance_visit(node.object, api_symbols)
        return False

    ## Visit the nodes, check if matches, and execute handler if it does
    def _recurrance_visit(self, node, api_symbols):

        # No more objects to look through
        if 'Identifier' == node.type:
            if node.name in api_symbols:
                return node.name

        # MemberExpression; maybe more objects
        elif 'MemberExpression' == node.type:
            if node.property.name in api_symbols:
                #self._memb_expr_handler(node)

                return_val = node.property.name
                tmp = self._recursive_check_objects(node, api_symbols)

                if tmp:
                    return_val = tmp + '.' + return_val

                return return_val

        # CallExpression; maybe more objects
        elif 'CallExpression' == node.type:
            if node.callee.name in api_symbols:
                #self._call_expr_handler(node)

                return_val = node.callee.name
                tmp = self._recursive_check_objects(node.callee, api_symbols)

                if tmp:
                    return_val = tmp + '.' + return_val

                return return_val
        return False
    ##################################################

    def _filter_parent_API(self, arg):
        if len(arg.split('.')[0]) == 1:
            arg = arg.split('.')
            arg.pop(0)
            arg = '.'.join(arg)
        return arg

    # FIRST VISIT: visit node, check if matches, and check for objects
    def visit(self, node, api_symbols):

        #MemberExpression
        if 'MemberExpression' == node.type:
            if node.property.name == self._property_name:

                return_val = node.property.name
                tmp = self._recursive_check_objects(node, api_symbols)

                if tmp:
                    return_val = tmp + '.' + return_val
                    return_val = self._filter_parent_API(return_val)

                return return_val

        # CallExpression
        if 'CallExpression' == node.type:
            if node.callee.name == self._property_name:

                return_val = node.callee.name
                tmp = self._recursive_check_objects(node.callee, api_symbols)

                if tmp:
                    return_val = tmp + '.' + return_val
                    return_val = self._filter_parent_API(return_val)

                return return_val
        return False


################################################################################
def uniquifyList(seq, idfun=None):
   # order preserving
   if idfun is None:
       def idfun(x): return x
   seen = {}
   result = []
   for item in seq:
       marker = idfun(item)
       if marker in seen: continue
       seen[marker] = 1
       result.append(item)
   return result


################################################################################
def worker_process(input_file):

    filename = input_file.split('/')[-1]

    # Try getting the AST using esprima, bail if non-JS syntax
    try:
        with open(input_file) as f:
            ast = esprima.parseScript(f.read())

    except esprima.error_handler.Error as e:
        return False, filename

    # Create an element using that AST
    el = Element(ast)
    for entry in api_symbols:
        visitor = MatchPropertyVisitor(entry)
        el.accept(visitor)

    # Walk down the AST (breadth-first)
    symbol_counter, extended_symbol_counter, node_dict = el.walk(api_symbols, filename)

    return True, extended_symbol_counter

################################################################################
if __name__ == '__main__':

    print("Initialized to use {} workers.".format(WORKERS))

    # Extract all symbols from the generated "Symbols of Interest" list,
    #   and flatten all api symbols into a single list (for efficiency)
    with open(SYM_LIST, encoding='utf-8') as f:
        api_list = json.loads(f.read())

    print("Looking in \'{}\' for the API list...".format(SYM_LIST))
    api_symbols = [val for sublist in api_list.values() for val in sublist];
    api_symbols = uniquifyList(api_symbols)
    print("Success.")

    # Get file list from data directory
    print("Looking in \'{}\' for all .txt files...".format(JS_SOURCE_FILES))

    file_list = glob.glob(JS_SOURCE_FILES + "/*")
    file_list_size = len(file_list)

    print("Success. Found {} files.".format(file_list_size))
    print("Begin iterating over the files to get symbol info.")
    print("-" * 80)

    pbar = tqdm(total=file_list_size)

    # Storage queue
    symbol_counts = multiprocessing.Queue()

    # Fail list (don't need a queue here)
    fails_list = multiprocessing.Queue()

    # Callback (add to queue)
    def log_result(result):
        pbar.update(1)
        if result[0]:
            symbol_counts.put(result[1])
        else:
            fails_list.put(result[1])

    # Process queue thread
    def test():
        counter = 0
        buffer_list = []

        def dump_files(buffer_list):
            from math import ceil
            df = pd.DataFrame(buffer_list)
            filename = OUTPUT_DIR + OUTPUT_FILE + "_" + str(ceil(counter/BATCH_SIZE)) + '.parquet'
            df.to_parquet(filename)
            return []

        while counter + fails_list.qsize() < file_list_size:
            try:
                # Attempt to get data from the queue. Note that
                # symbol_counts.get() will block this thread's execution
                # until data is available
                data = symbol_counts.get()
            except queue.Empty:
                pass
            except multiprocessing.TimeoutError:
                pass
            else:
                counter += 1;
                buffer_list.append(data)

                if counter % BATCH_SIZE == 0:
                    buffer_list = dump_files(buffer_list)

        buffer_list = dump_files(buffer_list)


    # Setup thread pool
    pool = multiprocessing.Pool(WORKERS)

    # Queue thread
    re = pool.apply_async(test, args=())

    # Individual file threads
    for filename in file_list:
        re = pool.apply_async(worker_process, args=(filename,), callback=log_result)

    pool.close()
    pool.join()

    print("Success.\n\nDONE SCRIPT!")

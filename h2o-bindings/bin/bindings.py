#!/usr/bin/env python  -- encoding: utf-8
#
# This is a helper module for scripts that auto-generate bindings for different languages.
# Usage:
#     import bindings as b
#     b.init(language="C#", output_dir="CSharp")
#     for schema in b.schemas():
#         name = schema["name"]
#         b.write_to_file("schemas/%s.cs" % name, gen_file_from_schema(schema))
#
from __future__ import print_function
from __future__ import division
from collections import defaultdict
import argparse
import atexit
import codecs
import errno
import os
import pprint
import re
import requests
import shutil
import sys
import textwrap
import time


class TypeTranslator:
    """
    Helper class to assist translating H2O types into native types of your target languages. Typically the user extends
    this class, providing the types dictionary, as well as overwriting any lambda-functions.
    """
    def __init__(self):
        # This is a conversion dictionary for simple types that have no schema
        self.types = {
            "byte": "byte", "short": "short", "int": "int", "long": "long",
            "float": "float", "double": "double", "string": "string", "boolean": "boolean",
            "Polymorphic": "Object", "Object": "Object"
        }
        self.make_array = lambda itype: itype + "[]"
        self.make_array2 = lambda itype: itype + "[][]"
        self.make_map = lambda ktype, vtype: "Map<%s,%s>" % (ktype, vtype)
        self.make_key = lambda itype, schema: schema
        self.make_enum = lambda schema: schema
        self.translate_object = lambda otype, schema: schema
        self._mem = set()  # Store all types seen, for debug purposes

    def translate(self, h2o_type, schema):
        if config["verbose"]:
            self._mem.add((h2o_type, schema))
        if h2o_type.endswith("[][]"): 
            return self.make_array2(self.translate(h2o_type[:-4], schema))
        if h2o_type.endswith("[]"):
            return self.make_array(self.translate(h2o_type[:-2], schema))
        if h2o_type.startswith("Map<"):
            t1, t2 = h2o_type[4:-1].split(",", 2)  # Need to be fixed once we have keys with commas...
            return self.make_map(self.translate(t1, schema), self.translate(t2, schema))
        if h2o_type.startswith("Key<"):
            return self.make_key(self.translate(h2o_type[4:-1], schema), schema)
        if h2o_type == "enum":
            return self.make_enum(schema)
        if schema is None:
            if h2o_type in self.types:
                return self.types[h2o_type]
            else:
                return h2o_type
        return self.translate_object(h2o_type, schema)

    def vprint_translation_map(self):
        if config["verbose"]:
            print("\n" + "-"*80)
            print("Type conversions done:")
            print("-"*80)
            for t, s in sorted(self._mem):
                print("(%s, %s)  =>  %s" % (t, s, self.translate(t, s)))
            print()


def init(language, output_dir, clear_dir=True):
    """
    Entry point for the bindings module. It parses the command line arguments and verifies their
    correctness.
      :param language -- name of the target language (used to show the command-line description).
      :param output_dir -- folder where the bindings files will be generated. If the folder does
        not exist, it will be created. If it does exist, it will be cleared first. This folder is 
        relative to ../src-gen/main/.
      :param clear_dir -- if True (default), the target folder will be cleared before any new 
        files created in it.
    """
    config["start_time"] = time.time()
    print("Generating %s bindings... " % language, end="")
    sys.stdout.flush()

    this_module_dir = os.path.dirname(os.path.realpath(__file__))
    default_output_dir = os.path.abspath(this_module_dir + "/../src-gen/main/" + output_dir)

    # Parse command-line options
    parser = argparse.ArgumentParser(
        description="""
        Generate %s REST API bindings (with docs) and write them to the filesystem.  
        Must attach to a running H2O instance to query the interface.""" % language,
    )
    parser.add_argument("-v", "--verbose", help="Verbose output", action="store_true")
    parser.add_argument("--usecloud", metavar="IP:PORT", default="localhost:54321",
                        help="Address of an H2O server (defaults to localhost:54321)")
    # Note: Output folder should be in build directory, however, Idea has problems to recognize them
    parser.add_argument("--dest", metavar="DIR", default=default_output_dir,
                        help="Destination directory for generated bindings")
    args = parser.parse_args()
    
    # Post-process the options
    base_url = args.usecloud
    if not(base_url.startswith("http://") or base_url.startswith("https://")):
        base_url = "http://" + base_url
    if not(base_url.endswith("/")):
        base_url += "/"
    config["baseurl"] = base_url
    config["verbose"] = args.verbose
    config["destdir"] = os.path.abspath(args.dest)
    vprint("\n\n")

    # Attempt to create the output directory
    try:
        vprint("Output directory = " + config["destdir"])
        os.makedirs(config["destdir"])
    except OSError as e:
        if e.errno != errno.EEXIST:
            print("Cannot create directory " + config["destdir"])
            print("Error %d: %s" % (e.errno, e.strerror))
            sys.exit(6)

    # Clear the content of the output directory. Note: deleting the directory and then recreating it may be
    # faster, but it creates side-effects that we want to avoid (i.e. clears permissions on the folder).
    if clear_dir:
        try:
            vprint("Deleting contents of the output directory...")
            for filename in os.listdir(config["destdir"]):
                filepath = os.path.join(config["destdir"], filename)
                if os.path.isdir(filepath):
                    shutil.rmtree(filepath)
                else:
                    os.unlink(filepath)
        except Exception as e:
            print("Unable to remove file %s: %r" % (filepath, e))
            sys.exit(9)

    # Check that the provided server is accessible, then print its status (if in --verbose mode).
    json = _request_or_exit("LATEST/About")
    l1 = max(len(e["name"]) for e in json["entries"])
    l2 = max(len(e["value"]) for e in json["entries"])
    ll = max(29 + len(config["baseurl"]), l1 + l2 + 2)
    vprint("-"*ll)
    vprint("Connected to an H2O instance " + config["baseurl"] + "\n")
    for e in json["entries"]:
        vprint(e["name"] + ":" + " "*(1+l1 - len(e["name"])) + e["value"])
    vprint("-"*ll)
    

def vprint(msg, pretty=False):
    """
    Print the provided string {msg}, but only when the --verbose option is on.
      :param msg     String to print.
      :param pretty  If on, then pprint() will be used instead of the regular print function.
    """
    if not config["verbose"]:
        return
    if pretty:
        pp(msg)
    else:
        print(msg)


def wrap(msg, indent, indent_first=True):
    """
    Helper function that wraps msg to 120-chars page width. All lines (except maybe 1st) will be prefixed with
    string {indent}. First line is prefixed only if {indent_first} is True.
      :param msg: string to indent
      :param indent: string that will be used for indentation
      :param indent_first: if True then the first line will be indented as well, otherwise not
    """
    wrapper.width = 120
    wrapper.initial_indent = indent
    wrapper.subsequent_indent = indent
    msg = wrapper.fill(msg)
    return msg if indent_first else msg[len(indent):]


def endpoints(full=False):
    """
    Return the list of REST API endpoints.
      :param full: if True, then the complete response to .../endpoints is returned (including the metadata)
    """
    json = _request_or_exit("/LATEST/Metadata/endpoints")
    if full:
        return json
    else:
        assert "routes" in json, "Unexpected result from /LATEST/Metadata/endpoints call"
        return json["routes"]


def endpoint_groups():
    """
    Return endpoints, grouped by the class which handles them
    """
    classname_pattern = re.compile(r"/(?:\d+|LATEST)/(\w+)")
    groups = defaultdict(list)
    for e in endpoints():
        mm = classname_pattern.match(e["url_pattern"])
        assert mm, "Cannot determine class name in URL " + e["url_pattern"]
        classname = mm.group(1)
        groups[classname].append(e)
    return groups


def schemas(full=False):
    """
    Return the list of H₂O schemas.
      :param full: if True, then the complete response to .../schemas is returned (including the metadata)
    """
    json = _request_or_exit("/LATEST/Metadata/schemas")
    if full:
        return json
    else:
        assert "schemas" in json, "Unexpected result from /LATEST/Metadata/schemas call"
        return json["schemas"]


def schemas_map():
    """
    Returns a dictionary of H₂O schemas, indexed by their name.
    """
    m = {}
    for schema in schemas():
        m[schema["name"]] = schema
    return m


def model_builders():
    """
    Return the list of models and their parameters.
    """
    json = _request_or_exit("/LATEST/ModelBuilders")
    assert "model_builders" in json, "Unexpected result from /LATEST/ModelBuilders call"
    return json["model_builders"]


def enums():
    """
    Return the dictionary of H₂O enums, retrieved from data in schemas(). For each entry in the dictionary its key is
    the name of the enum, and the value is the set of all enum values.
    """
    enumset = defaultdict(set)
    for schema in schemas():
        for field in schema["fields"]:
            if field["type"] == "enum":
                enumset[field["schema_name"]].update(field["values"])
    return enumset


def write_to_file(filename, content):
    """
    Writes content to the given file. The file's directory will be created if needed.
      :param filename: name of the output file, relative to the "destination folder" provided by the user
      :param content: iterable (line-by-line) that should be written to the file. Either a list or a generator. Each
                      line will be appended with a "\n". Lines containing None will be skipped.
    """
    if not config["destdir"]:
        print("{destdir} config variable not present. Did you forget to run init()?")
        sys.exit(8)
    abs_filename = os.path.abspath(config["destdir"] + "/" + filename)
    abs_filepath = os.path.dirname(abs_filename)
    if not os.path.exists(abs_filepath):
        try:
            os.makedirs(abs_filepath)
        except OSError as e:
            print("Cannot create directory " + abs_filepath)
            print("Error %d: %s" % (e.errno, e.strerror))
            sys.exit(6)
    with codecs.open(abs_filename, "w", "utf-8") as out:
        if type(content) in (str, unicode): content = [content]
        for line in content:
            if line is not None:
                out.write(line)
                out.write("\n")


# ----------------------------------------------------------------------------------------------------------------------
#   Private
# ----------------------------------------------------------------------------------------------------------------------
config = defaultdict(bool)  # will be populated during the init() stage
pp = pprint.PrettyPrinter(indent=4).pprint  # pretty printer
wrapper = textwrap.TextWrapper()
requests_memo = {}  # Simple memoization, so that we don't fetch same data more than once


def _request_or_exit(endpoint):
    """
    Internal function: retrieve and return json data from the provided endpoint, or die with an error message if the
    URL cannot be retrieved.
    """
    if endpoint[0] == "/":
        endpoint = endpoint[1:]
    if endpoint in requests_memo:
        return requests_memo[endpoint]

    if not config["baseurl"]:
        print("Configuration not present. Did you forget to run init()?")
        sys.exit(8)
    url = config["baseurl"] + endpoint
    try:
        resp = requests.get(url)
    except requests.exceptions.InvalidURL:
        print("Invalid url address of an H2O server: " + config["baseurl"])
        sys.exit(2)
    except requests.ConnectionError:
        print("Cannot connect to the server " + config["baseurl"])
        print("Please check that you have an H2O instance running, and its address is passed in " +
              "the --usecloud argument.")
        sys.exit(3)
    except requests.Timeout:
        print("Request timeout when fetching " + url + ". Check your internet connection and try again.")
        sys.exit(4)
    if resp.status_code == 200:
        try:
            json = resp.json()
        except ValueError:
            print("Invalid JSON response from " + url + " :\n")
            print(resp.text)
            sys.exit(5)
        if "__meta" not in json or "schema_type" not in json["__meta"]:
            print("Unexpected JSON returned from " + url + ":")
            pp(json)
            sys.exit(6)
        if json["__meta"]["schema_type"] == "H2OError":
            print("Server returned an error message for %s:" % url)
            print(json["msg"])
            pp(json)
            sys.exit(7)
        requests_memo[endpoint] = json
        return json
    else:
        print("[HTTP %d] Cannot retrieve %s" % (resp.status_code, url))
        sys.exit(1)


@atexit.register
def _report_time():
    if config["start_time"]:
        print("done (in %.3fs)" % (time.time() - config["start_time"]))

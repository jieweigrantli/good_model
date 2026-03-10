import os
import json
import importlib

def load_module(module_path):

	_, module_name = os.path.split(module_path)
	module_name = module_name.split('.')[0]

	# Loading the processing module
	spec = importlib.util.spec_from_file_location(module_name, module_path)
	module = importlib.util.module_from_spec(spec)

	spec.loader.exec_module(module)

	return module

def process(data, module_path):

	module = load_module(module_path)

	# Calling main fucntion from processing module
	outputs = module.main(data)

	return outputs
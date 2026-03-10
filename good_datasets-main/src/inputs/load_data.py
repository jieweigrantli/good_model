import os
import json
import time

import pandas as pd

def _print(string, disp = True):

    if disp:

        print(string)

def load(codex_path, verbose = False):
	'''
	Load data based on a codex file. the codex file will be a JSON of the below format:

	[
		{
			"name": <variable name>,
			"file": <file name>,
			"kwargs": {
				<argument>: <value>,
				<argument>: <value>
			}
		}
	]

	Files must be CSV or Excel types. Files will be loaded using the appropriate Pandas
	method with **kwargs as inputs.

	Output will be a dictionary of {<name>: <DataFrame>}
	'''

	t0 = time.time()

	# Getting file parts for codex

	codex_directory, codex_file = os.path.split(codex_path)

	# Loading codex file

	with open(codex_path, 'r') as file:

		codex = json.load(file)

	# Loading in data

	data = {}

	for item in codex:

		extension = item['file'].split('.')[-1]

		load_path = os.path.join(codex_directory, item['file'])

		if extension == 'csv':

			data[item['name']] = pd.read_csv(load_path, **item['kwargs'])

		elif (extension == 'xlsx') or (extension == 'xls'):

			data[item['name']] = pd.read_excel(load_path, **item['kwargs'])

		else:

			raise RawDataFileTypeException

	_print(f'Data loaded: {time.time() - t0:.4} seconds', disp = verbose)

	return data

class RawDataFileTypeException(Exception):

	"Raw data input files must be .csv, .xls, or .xlsx"

	pass
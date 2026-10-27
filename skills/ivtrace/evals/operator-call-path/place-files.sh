#!/usr/bin/env bash
# Copies this case's input export into the working directory. The export is not kept in the repository: it is
# tens of megabytes and holds data from the database it came from. To run the case, export the run named in
# case.yaml from Internals Viewer (Query > Export to file) against the InternalsViewerDemo database and save it
# under files/ with the name the prompt uses. The expected numbers in case.yaml came from such an export.
set -eu
if [ -d "$(dirname "$0")/files" ]; then
  cp -R "$(dirname "$0")/files/." .
else
  echo "No files/ folder beside this case: add the export the prompt names before running it" >&2
  exit 1
fi

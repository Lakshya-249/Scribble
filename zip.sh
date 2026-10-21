#!/bin/bash

zip -r project.zip . -x ".venv/*" -x "patch/*" -x "fonts/*" -x "temp/*" -x ".git/*"

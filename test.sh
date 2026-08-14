#!/bin/bash

base64 -w 0 images/gpt.png | jq -Rs '{image: .}' | \
curl -s -X POST 'http://localhost:6000/ocr' \
  -H 'Content-Type: application/json' \
  -d @-
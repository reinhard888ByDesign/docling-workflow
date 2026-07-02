#!/bin/bash
# Resume Batch 25 — scheduled via at/cron
# Input: /data/dispatcher-temp/batch_input_rest.txt (1491 total, 444 done, 1047 left)

curl -s -X POST http://localhost:8765/api/batch/start \
  -H "Content-Type: application/json" \
  -d '{"input": "/data/dispatcher-temp/batch_input_rest.txt", "ocr_mode": "hybrid", "output_mode": "classify-only"}' \
  -o /tmp/batch_25_resume_result.json

echo "Batch 25 resumed at $(date)" >> /tmp/batch_25_resume.log
cat /tmp/batch_25_resume_result.json >> /tmp/batch_25_resume.log

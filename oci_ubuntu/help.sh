#!/bin/bash

HUBRAUM_INST=`curl -s  http://localhost:10002/endpoint | jq -r '.endpoint'`
ORCHESTRATOR="${HUBRAUM_INST}/LTECamOrchestrator"
#echo $ORCHESTRATOR
API_KEY_TMP=`curl -s  http://localhost:10002/api_key | grep API-KEY | cut -d ":" -f2 | sed 's/"//g'`
API="API-KEY: $API_KEY_TMP"

echo $API
echo $ORCHESTRATOR


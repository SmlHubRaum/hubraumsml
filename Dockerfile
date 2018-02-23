FROM smartmobilelabs/hubraumonboarding:jenkins-Orchestrator_on_docker-93

CMD startup --base-port 9000 --orchestrator-integration-license akN2pxDlJelnjvEm3r+hWd/LGH2FH5G3ZuUi35CP8OM= --orchestrator-integration-mode --min-port 50001 --max-port 50005 --random-port-range 2

#FROM smartmobilelabs/sml_orchestrator_development:jenkins-Orchestrator_on_docker-93
FROM smartmobilelabs/sml_orchestrator_development:hubraum

COPY server.xml_template /usr/local/tomcat/conf
COPY PacketDispatcher /usr/local/tomcat/native-libs

CMD startup --base-port 9000 --orchestrator-integration-license akN2pxDlJelnjvEm3r+hWd/LGH2FH5G3ZuUi35CP8OM= --orchestrator-integration-mode --min-port 16000 --max-port 16030 --random-port-range 10


import sys, time
import json
from ensclient import ENSClient, ENSHttpSession

JSON_HEADER = {'content-type': 'application/json'}

report = {"status": "down", "cloudlet": "", "latency": 0.0}
#aseemedgecr/dk-service-06
# Auth with  ENS platform
c = ENSClient("jmorgade.sml-evo-dev6")
print("mge: discovered")
#c = ENSClient("markmiller.aseem_app-dep-3")
cumulative_time = 0.0

if not c.init():
    print("Failed to initialize")
    report["status"] = "auth-error"
else:
    print("mge: trying to connect")
    s = c.connect("sml-evo-dev.evo-rest")

    import pdb
    pdb.set_trace()


    #s = c.connect("aseem_app-dep-3.ping")
    if not s:
        print("Failed to connect to microservice")
        report["status"] = "sess-error"
    else:
        report["status"] = "active"
        report["cloudlet"] = s.cloudlet
	print("Start GET ...");
        for i in range(1, 3):
            start_time = time.time()
            rsp = s.request(api = '/LTECamOrchestrator', method = 'GET', header = JSON_HEADER)
	    print rsp.text
            #rsp = s.request(api = '/', method = 'GET', header = JSON_HEADER)
            if rsp != None:
                print("API response: {}".format(rsp))
                end_time = time.time()
                print("Latency = {}".format(end_time - start_time))
                cumulative_time += (end_time - start_time)
                sys.stdout.flush()
                time.sleep(1)
            else:
                time.sleep(1)
        report["latency"] = cumulative_time / 10.0

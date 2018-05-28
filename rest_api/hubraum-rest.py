from flask import Flask, request
from flask_restful import Resource, Api
import sys
import time
import json
from pprint import pprint

from flask import Response

app = Flask(__name__)
api = Api(app)


class TodoSimple(Resource):
    def get(self):
	global data
        return { 'API-KEY' : data["microservices"][0]["httpGateway"][0]["accessToken"] }
        #return { "dsdksldksl" }

class TodoSimple2(Resource):
    def get(self):
	global data
        return { 'endpoint' : data["microservices"][0]["httpGateway"][0]["endpoint"] }

class TodoSimple3(Resource):
    def get(self,id):

#	data = json.load(open('/home/docker/work/remote-call/provisining.json'))
	global data
	for song in data["microservices"][0]["networkBinding"]:
		#print song["networkId"]
		if song["networkId"] == str('evo-udp'+str(id)):
			#print song["networkId"]
			return { 'port' :  song["endpoint"].split(':')[2] }
        return { 'port' : '-1' } #data["microservices"][0]["networkBinding"][0] }

class TodoSimple4(Resource):
    def get(self):
	global data
        return  {'microservices' : data["microservices"]}


api.add_resource(TodoSimple, '/api_key')
api.add_resource(TodoSimple2, '/endpoint')
api.add_resource(TodoSimple3, '/network_binding/<int:id>', endpoint ='network_binding')
api.add_resource(TodoSimple4, '/download')


if __name__ == '__main__':
    if(len(sys.argv) > 1):
        run_port = sys.argv[1]
    else:
        run_port = 10002
    data = json.load(open('/home/docker/work/remote-call-dev/provisining.json'))
    #data = json.load(open('provisioning.json'))
    app.run(host='0.0.0.0',port=int(run_port), debug=True)

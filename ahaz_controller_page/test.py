import json

import requests

url = "http://10.13.0.2:5000"

namespace = {"teamname": "1"}
podresult = requests.get(url + "/get_pods_namespace", json=namespace)
podresultlist = json.loads(podresult.text)
print(podresult.text)

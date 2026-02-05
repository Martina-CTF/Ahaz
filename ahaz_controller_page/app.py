import json
import time
from os import environ

import pymysql  # pyright: ignore[reportMissingModuleSource]
import requests
from flask import Flask, Response, redirect, render_template, request

# TODO: Refactor this to be more in-line with modern Ahaz code

app = Flask(__name__, template_folder="templates")

apiIP = environ.get("API_IP", "10.13.0.2")
apiPort = environ.get("API_PORT", "5000")
serverIP = environ.get("DB_IP", "10.13.0.3")
databasename = environ.get("DB_DBNAME", "ahaz")
user = environ.get("DB_USERNAME", "dbeaver")
password = environ.get("DB_PASSWORD", "dbeaver")
k8s_ip_range = environ.get("K8S_IP_RANGE", "10.42.0.0 255.255.0.0")
admin_vpn_name = environ.get("ADMIN_VPN_NAME", "admin")
url = "http://" + apiIP + ":" + str(apiPort)


def sanitize(input):
    input = input.replace("'", "")
    input = input.replace('"', "")
    return input


@app.route("/")
def hello():
    return "Hello World!"


@app.route("/register")
def register():
    conn = pymysql.connect(host=serverIP, port=3306, user=user, passwd=password, database=databasename)
    cursor = conn.cursor()
    cursor.execute("SELECT * from teams JOIN vpn_map USING (teamID)")  # Gets teamID,teamname,port
    rows_team = cursor.fetchall()
    teams = []
    cursor.execute("SELECT challengename FROM image")
    rows_challenge = cursor.fetchall()
    for i in rows_team:
        team = {
            "id": str(i[0]),
            "name": str(i[1]),
            "port": str(i[2]),
        }
        teams.append(team)
        # returnable += "}<br>"
    # print(returnable)
    # print(rows_team)
    # print(teams)
    return render_template("register.html", teams=teams, challenges=rows_challenge)


@app.route("/teams")
def teams():
    conn = pymysql.connect(host=serverIP, port=3306, user=user, passwd=password, database=databasename)
    cursor = conn.cursor()
    cursor.execute("SELECT * from teams JOIN vpn_map USING (teamID)")  # Gets teamID,teamname,port
    rows_team = cursor.fetchall()
    teams = []
    cursor.execute("SELECT challengename FROM image")
    rows_challenge = cursor.fetchall()
    for i in rows_team:
        namespace = {"teamname": str(i[1])}
        podresult = requests.get(url + "/get_pods_namespace", json=namespace)
        # print("podresult")
        # print(podresult.text)
        podresultlist = json.loads(podresult.text)
        # print(podresultlist)
        # for j in podresultlist:
        #    returnable += "<br>----"
        #    returnable += str(j["status"])+","
        #    returnable += str(j["name"])+","
        #    returnable += str(j["ip"]) +"]"
        internalIP = ""
        for j in podresultlist:
            if j["name"] == "vpn-container-pod":
                internalIP = str(j["ip"])
                podresultlist.remove(j)
                continue
        team = {
            "id": str(i[0]),
            "name": str(i[1]),
            "port": str(i[2]),
            "containers": podresultlist,
            "internalIP": internalIP,
        }
        teams.append(team)
        # returnable += "}<br>"
    # print(returnable)
    # print(rows_team)
    # print(teams)
    return render_template("teams.html", teams=teams, challenges=rows_challenge)


@app.route("/teams/get_ovpn_cert", methods=["POST"])
def getOVPNCert():
    # this means that admins need to be registered to each ovpn instance/namespace
    # we cannot use user certs, because only one cert can be used at a time to connect to the namespace,
    # meaning this might kick out a user if used

    print("attempting to getOVPNCert")
    try:
        url_vpn = "http://" + apiIP + ":" + apiPort + "/get_user"
        print(url_vpn)
        teamId = request.form.get("teamname")
        print(request.form.values)
        print(teamId)
        userId = admin_vpn_name
        jsonz = {"teamname": teamId, "username": userId}
        ovpn_cert = requests.get(url_vpn, json=jsonz)
        if ovpn_cert.text == "" or ovpn_cert.text == "null":  # if there is no cert, attempt to register user
            print("recognised that ovpn cert is empty")
            try:
                # attempt to register admin user
                url_vpn_register = "http://" + apiIP + ":" + apiPort + "/add_user"
                json = {"teamname": teamId, "username": userId}
                requests.post(url_vpn_register, json=json)
                # Retreive admin cert
                jsonz = {"teamname": teamId, "username": userId}
                ovpn_cert = requests.get(url_vpn, json=jsonz)
            except Exception as e:
                print("encountered an error while registering admin user")
                print(e)
                return "Failed registering admin user"
        return Response(
            ovpn_cert.text,
            mimetype="text/plain",
            headers={"Content-disposition": f"attachment; filename=ahaz_{teamId}.ovpn"},
        )
    except Exception as e:
        print("encountered an error or timeout")
        print(e)
        return "<h1>encountered an error or timeout</h1>"


@app.route("/teams/start_challenge", methods=["POST"])
def startChallenge():
    try:
        url_chal = "http://" + apiIP + ":" + apiPort + "/start_challenge"
        print(url_chal)
        teamId = request.form.get("teamname")
        challengename = request.form.get("challengename")
        print(request.form.values)
        jsonz = {"teamname": teamId, "challengename": challengename}
        print(jsonz)
        requests.post(url=url_chal, json=jsonz)
        return redirect("/teams", code=302)
    except Exception as e:
        print("encountered an error or timeout")
        print(e)
        return "<h1>encountered an error or timeout</h1>"


@app.route("/teams/stop_challenge", methods=["POST"])
def stopChallenge():
    try:
        url_chal = "http://" + apiIP + ":" + apiPort + "/stop_challenge"
        print(url_chal)
        teamId = request.form.get("teamname")
        challengename = request.form.get("challengename")
        print(request.form.values)
        jsonz = {"teamname": teamId, "challengename": challengename}
        print(jsonz)
        requests.post(url=url_chal, json=jsonz)
        return redirect("/teams", code=302)
    except Exception as e:
        print("encountered an error or timeout")
        print(e)
        return "<h1>encountered an error or timeout</h1>"


@app.route("/teams/reboot_challenge", methods=["POST"])
def rebootChallenge():  # wrote it, but it crashes, should be worked on next
    try:
        url_chal = "http://" + apiIP + ":" + apiPort + "/stop_challenge"
        print(url_chal)
        teamId = request.form.get("teamname")
        challengename = request.form.get("challengename")
        print(request.form.values)
        jsonz = {"teamname": teamId, "challengename": challengename}
        print(jsonz)
        requests.post(url=url_chal, json=jsonz)
        url_chal = "http://" + apiIP + ":" + apiPort + "/start_challenge"
        isterminating = True
        namespace = {"teamname": teamId}
        while isterminating:
            podresult = requests.get(url + "/get_pods_namespace", json=namespace)
            podexists = False
            for i in podresult:
                if i["name"] == challengename:  # pyright: ignore[reportCallIssue, reportArgumentType]
                    podexists = True
            if not podexists:
                isterminating = False
            else:
                time.sleep(3)
        requests.post(url=url_chal, json=jsonz)
        return redirect("/teams", code=302)
    except Exception as e:
        print("encountered an error or timeout")
        print(e)
        return "<h1>encountered an error or timeout</h1>"


@app.route("/register/team", methods=["POST"])
def register_team():
    try:
        url_team = "http://" + apiIP + ":" + apiPort + "/gen_team_lazy"
        teamname = request.form.get("teamname")
        teamname = sanitize(teamname)
        jsonz = {"teamname": teamname}
        requests.post(url_team, json=jsonz)
        print(url_team)
        print(jsonz)
        return redirect("/register", code=302)
    except Exception as e:
        print("encountered an error or timeout")
        print(e)
        return "<h1>encountered an error or timeout</h1>"


@app.route("/register/user", methods=["POST"])
def register_user():
    try:
        url_user = "http://" + apiIP + ":" + apiPort + "/add_user"
        teamname = request.form.get("teamname")
        teamname = sanitize(teamname)
        username = request.form.get("username")
        username = sanitize(username)
        jsonz = {"teamname": teamname, "username": username}
        requests.post(url_user, json=jsonz)
        print(url_user)
        print(jsonz)
        return redirect("/register", code=302)
    except Exception as e:
        print("encountered an error or timeout")
        print(e)
        return "<h1>encountered an error or timeout</h1>"


@app.route("/users", methods=["get"])
def get_users():
    conn = pymysql.connect(host=serverIP, port=3306, user=user, passwd=password, database=databasename)
    cursor = conn.cursor()
    # Gets teamID,teamname,port
    cursor.execute("select teamID, username, name from vpn_storage JOIN teams USING (teamID) ORDER by name;")
    rows_team = cursor.fetchall()  # gets teamID, username, teamname
    for i in rows_team:
        print(i)
    return render_template("users.html", teams=rows_team)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
    # TODO, dabūt resursu lietojumu no konteineriem, un iekrāso to, atkarībā no tā, cik ļoti lieto resursus.

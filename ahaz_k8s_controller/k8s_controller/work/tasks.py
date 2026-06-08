import certmanager
import controller
import dboperator


def gen_cert(team_id: str, port: int, public_domainname: str, certdir: str):
    certmanager.gen_team(team_id, public_domainname, port, "tcp", certdir)


def create_namespace(team_id: str):
    controller.create_team_namespace(team_id)


def create_vpn_container(team_id: str):
    controller.create_team_vpn_container(team_id)


def expose_vpn_container(team_id: str, port: int):
    controller.expose_team_vpn_container(team_id, port)


def insert_db(team_id: str, port: int):
    dboperator.insert_team_into_db(team_id)
    dboperator.insert_vpn_port_into_db(team_id, port)


def register_user(team_id: str, user_id: str):
    controller.register_user_ovpn(team_id, user_id)


def insert_user_db(team_id: str, user_id: str):
    config = controller.obtain_user_ovpn_config(team_id, user_id)
    dboperator.insert_user_vpn_config(team_id, user_id, config)

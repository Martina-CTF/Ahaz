# Database Schema

The Ahaz controller uses a SQL databse to store information about tasks and its components (notably, the containers and networks connecting them). The database schema is as follows:

```sql
CREATE table teams(
    name varchar(255), 
    teamID INT NOT NULL AUTO_INCREMENT,
    PRIMARY KEY (teamID)
);

CREATE table vpn_map(
    teamID int, 
    port int
);

CREATE table vpn_storage(
    teamID int,
    username varchar(255),
    config varchar(8000)
);

CREATE table challenges(
    name varchar(255),
    ctfd_desc varchar(1024),
    ctfd_score int,
    ctfd_type varchar(255)
);

CREATE table pods(
    name varchar(255),
    k8s_name varchar(50),
    image varchar(1024),
    ram varchar(32),
    cpu int, 
    visible_to_user bool
);

CREATE table net_rules(
    name varchar(255),
    netname varchar(255),
    k8s_name varchar(50)
);

CREATE table env_vars(
    name varchar(255),
    k8s_name varchar(50),
    env_var_name varchar(1024),
    env_var_value varchar(1024)
);
```

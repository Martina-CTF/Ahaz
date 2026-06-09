echo "** Creating default DB and users"

# FIXME: Better names, add indexes, primary keys, relations and constraints
# TODO: Extract this out into a sql file, add migrations
mysql -u root -p$MYSQL_ROOT_PASSWORD --execute \
"DELETE FROM mysql.user WHERE User='root' AND Host NOT IN ('localhost', '127.0.0.1', '::1');
use $MYSQL_DATABASE;
CREATE table teams(name varchar(255), teamID INT NOT NULL AUTO_INCREMENT,PRIMARY KEY (teamID));
CREATE table vpn_map(teamID int, port int);
CREATE table vpn_storage(teamID int,username varchar(255),config varchar(8000));
-- CREATE table image(repo varchar(255), name varchar(255), tag varchar(255), challengename varchar(255) );
CREATE table challenges(name varchar(255),ctfd_desc varchar(1024),ctfd_score int,ctfd_type varchar(255));
CREATE table pods(name varchar(255),k8s_name varchar(50),image varchar(1024),ram varchar(32),cpu int, visible_to_user bool);
CREATE table net_rules(name varchar(255),netname varchar(255),k8s_name varchar(50));
CREATE table env_vars(name varchar(255),k8s_name varchar(50),env_var_name varchar(1024),env_var_value varchar(1024));
CREATE table register_status(name varchar(255),user varchar(255),state int,timestamp bigint)
"

echo "** Finished creating default DB and users"

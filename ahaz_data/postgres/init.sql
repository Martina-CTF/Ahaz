CREATE TABLE teams (
  id TEXT PRIMARY KEY,
  namespace TEXT NOT NULL UNIQUE CHECK (
    length(namespace) > 0
    AND length(namespace) <= 255
    AND namespace ~ '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'
  ),
  vpn_config TEXT, -- May migrate towards specific parameters in the future
  vpn_port INTEGER NOT NULL UNIQUE CHECK (
    vpn_port > 0
    AND vpn_port < 65536
  )
  -- TODO: "deleting" flag?
);

CREATE TABLE users (
  id TEXT NOT NULL,
  team_id TEXT NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
  vpn_config TEXT, -- May migrate towards specific parameters in the future
  PRIMARY KEY (id, team_id)
  -- TODO: "deleting" flag?
);

CREATE INDEX idx_users_team_id ON users (team_id);

-- For efficient lookups of users by team_id
CREATE TABLE tasks (
  name TEXT PRIMARY KEY,
  -- Maybe move this out to a seperate table to ensure pod names are unique
  -- across tasks (we need that for corrent lookups in the event system!)
  pods JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(pods) = 'array'),
  networks JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(networks) = 'array')
);

-- Legacy, will be removed in the future.
CREATE TABLE register_status (
  name varchar(255),
  username varchar(255),
  state int,
  timest bigint
);

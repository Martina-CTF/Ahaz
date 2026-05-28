# Ahaz

Ahaz is a CTF container task solution allowing teams to deploy instances of tasks in a Kubernetes cluster.

# Setup

There are two ways to deploy Ahaz - via Docker Compose (outside Kubernetes deployment) or via Helm Chart (in-Kubernetes deployment).

## Docker Compose Deployment

The following environment variables must be set for Ahaz to function:

- `PUBLIC_DOMAINNAME` (Default: `ahaz.lan`), the domain name through which the Ahaz VPN pods will be addressed with. It should resolve to the server(s) that will host VPN pods.
- `K8S_IP_RANGE` (Default: `10.42.0.0 255.255.255.0`), an IP+Mask or CIDR string specifying the IP address range used within the cluster. It will be used to generate VPN certificates.

The following environment variables may be set to customise Ahaz functionality:

- `CERT_DIR_CONTAINER` (Default: `/certdir/`), specified the location of the VPN certificate directory within the container. Should only be changed if the ahaz_data directory mount is modified.
- `DB_IP` (Default: `10.33.0.3`), IP address at which the database may be accessed.
- `DB_DBNAME` (Default: `ahaz`), the name of the database to use.
- `DB_USERNAME` (Default: `dbeaver`), the username for connecting to the database.
- `DB_PASSWORD` (Default: `dbeaver`), the password for connecting to the database.
- `REDIS_URL` (Default: `redis://10.33.0.4:6379`), the connection URL for the Redis instance.
- `K8S_IMAGEPULLSECRET_NAME` (Default: `regcred`), the name of the Kubernetes secret used for pulling container images (useful for pulling task images from a private Docker registry).
- `K8S_IMAGEPULLSECRET_NAMESPACE` (Default: `default`), the namespace where the image pull secret is located.
- `TEAM_PORT_RANGE_START` (Default: `31000`), the starting port number for the range of ports allocated to teams.
- `TEAM_BACKUP_PORT_RANGE_START` (Default: `30500`), the starting port number for the backup port range.
- `TEAM_BACKUP_PORT_RANGE_END` (Default: `30999`), the ending port number for the backup port range.
- `LOGLEVEL` (Default: `DEBUG`), sets the logging level for the application.
- `OVPN_IMAGE` (Default: `lisenet/openvpn`), the Docker image to use for OpenVPN pods.
- `OVPN_TAG` (Default: `2.6.14`), the tag for the OpenVPN Docker image.

The deployment also necessitates a valid kubectl config file located in `ahaz_data/certs/config.yml`. Alternatively, you may modify the volume mount to mount a valid kubectl config file on `/certdir` on the image.

The controller can be started using `docker compose up`, much like any other Docker Compose project.

## Helm Deployment

The Helm deployment can be found in the [Helm chart repo](https://github.com/Martina-CTF/helm-charts). The Helm chart requires that you have [Kyverno](https://kyverno.io/) installed in your cluster.

In contrast to the Docker Compose deployment, the Helm chart will create a service account with the minimum required permissions to be able to perform full Ahaz functionality, permissions in namespaces are automatically added via Kyverno.

The deployment may be customised using the `values.yaml` file found inside the chart.

The chart can be deployed using `helm install ahaz oci://ghcr.io/martina-ctf/helm-charts/ahaz`, values for the chart can be found [here](https://github.com/Martina-CTF/helm-charts/blob/main/charts/ahaz/values.yaml).

## Development

For development purposes, a `kind` cluster can be created using `uv run create-cluster`. The cluster can later be removed using `uv run delete-cluster`.

The process also locally builds an Ahaz image and pushes it to the local registry, the image may be updated using `uv run build` or `uv run watch`, in which case the image will be rebuild on every file change in the project.

## Host Cluster

In addition to deploying Ahaz either through Docker Compose or through Helm, it is also necessary to configure the cluster which will host Ahaz namespaces.

As a bare minimum, it is necessary to apply the respective taints and labels to nodes that will be used to host the VPN and task pods.

The following taints may be applied to nodes:

- `ahaz-controller/node-role` - role of the node in Ahaz; may be either `PreferNoSchedule`, `NoSchedule` or `NoExecute` depending on the threat model
  - `vpn` - This node is provisioned to host VPN pods.
  - `task` - This node is provisioned to host task pods.
  - `shared` - This node is shared between both task and VPN pods.

The following labels may be applied to nodes:

- `ahaz-controller/node-role` - role of the node in Ahaz
  - `vpn` - This node may host VPN pods.
  - `task` - This node may host task pods.
  - `shared` - This node may host both task and VPN pods.
  - `unmanaged` - This node is not to be used by Ahaz.

### Examples

#### Single-node setup

```
kubectl label nodes <node-name> ahaz-controller/node-role=shared
```

#### Multi-node setup

On every VPN node:

```
kubectl taint nodes <node-name> ahaz-controller/node-role=NoExecute:vpn
kubectl label nodes <node-name> ahaz-controller/node-role=vpn
```

On every task node:

```
kubectl taint nodes <node-name> ahaz-controller/node-role=NoExecute:task
kubectl label nodes <node-name> ahaz-controller/node-role=task
```

# Updating/Inserting Challenges

To update or insert challenges into the Ahaz database, you may use a MySQL client to connect to the database and execute the necessary SQL commands.

If your deployment is running via Docker Compose, you may temporarily expose the database port by uncommenting the relevant lines in `docker-compose.yaml`:

```yaml
k8s_controller_db:
    ...
    networks:
        ...
#       default
#    ports:
#     - "3306:3306"
```

If your deployment is running via Helm, you may port-forward the database service to your local machine:

```
kubectl port-forward -n ahaz-system svc/ahaz-db 3306:3306
```

# License

Ahaz is distributed under [AGPL-3.0](./LICENSE).

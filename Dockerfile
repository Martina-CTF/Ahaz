FROM debian:latest
RUN apt-get update && apt-get install -y \
    openvpn \
    python3

COPY --from=docker:cli /usr/local/bin/docker /usr/local/bin/

RUN apt-get update && apt-get install -y \
    python3-jinja2 \
    python3-requests \
    python3-yaml \
    python3-kubernetes \
    python3-docker \
    python3-pymysql \
    python3-flask \
    python3-pip \
    python3-gunicorn
COPY install_kubectl.sh install_kubectl.sh
RUN chmod +x install_kubectl.sh
RUN ./install_kubectl.sh
RUN kubectl version --client
COPY ./test_kube /.kube
ENV KUBECONFIG=/.kube/config
COPY scripts scripts
COPY runguicorn.sh scripts/runguicorn.sh
WORKDIR scripts

#RUN python3 -m pip install -r requirements.txt
#CMD python3 cerserver.py
#CMD ["python3","cicd_cerserver.py"]
#RUN chmod +777 runguicorn.sh
#CMD ./runguicorn.sh
CMD python3 -m gunicorn -w 4 -b 0.0.0.0:5000 --timeout=360 cicd_cerserver:app

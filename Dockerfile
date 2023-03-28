# syntax=docker/dockerfile:experimental

FROM centos:centos7.9.2009

# NOTE(crag): NB_USER ARG for mybinder.org compat:
#             https://mybinder.readthedocs.io/en/latest/tutorials/dockerfile.html
ARG NB_USER=notebook-user
ARG NB_UID=1000
ARG PIP_VERSION
ARG PIPELINE_PACKAGE

RUN yum -y update && \
  yum -y install poppler-utils xz-devel which

# Note(yuming): Install gcc & g++ ≥ 5.4 for Detectron2 requirement
RUN yum -y update
RUN yum -y install centos-release-scl
RUN yum -y install devtoolset-7-gcc*
SHELL [ "/usr/bin/scl", "enable", "devtoolset-7"]

# Note(austin) Get a recent tesseract from this repo
# See https://tesseract-ocr.github.io/tessdoc/Installation.html
RUN yum-config-manager --add-repo https://download.opensuse.org/repositories/home:/Alexander_Pozdnyakov/CentOS_7/ && \
  rpm --import https://build.opensuse.org/projects/home:Alexander_Pozdnyakov/public_key && \
  yum -y update && \
  yum -y install tesseract

RUN yum -y update && \
  yum -y install libreoffice && \
  yum -y install openssl-devel bzip2-devel libffi-devel make git sqlite-devel && \
  curl -O https://www.python.org/ftp/python/3.8.15/Python-3.8.15.tgz && tar -xzf Python-3.8.15.tgz && \
  cd Python-3.8.15/ && ./configure --enable-optimizations && make altinstall && \
  cd .. && rm -rf Python-3.8.15* && \
  ln -s /usr/local/bin/python3.8 /usr/local/bin/python3

# create user with a home directory
ENV USER ${NB_USER}
ENV HOME /home/${NB_USER}

RUN groupadd --gid ${NB_UID} ${NB_USER}
RUN useradd --uid ${NB_UID}  --gid ${NB_UID} ${NB_USER}
USER ${NB_USER}
WORKDIR ${HOME}
RUN mkdir ${HOME}/.ssh && chmod go-rwx ${HOME}/.ssh \
  &&  ssh-keyscan -t rsa github.com >> /home/${NB_USER}/.ssh/known_hosts

ENV PYTHONPATH="${PYTHONPATH}:${HOME}"
ENV PATH="/home/${NB_USER}/.local/bin:${PATH}"

# COPY requirements/dev.txt requirements-dev.txt
COPY requirements/base.txt requirements-base.txt
RUN python3.8 -m pip install pip==${PIP_VERSION} \
  && pip3.8 install  --no-cache  -r requirements-base.txt \
  && pip3.8 install --no-cache "detectron2@git+https://github.com/facebookresearch/detectron2.git@v0.6#egg=detectron2"

# NOTE(Crag): work around annoying complaint that sometimes occurs about /usr/local/bin/pip not existing
USER root
RUN ln -s /home/notebook-user/.local/bin/pip /usr/local/bin/pip
USER ${NB_USER}

COPY --chown=${NB_USER}:${NB_USER} prepline_${PIPELINE_PACKAGE}/ prepline_${PIPELINE_PACKAGE}/
COPY --chown=${NB_USER}:${NB_USER} exploration-notebooks exploration-notebooks
COPY --chown=${NB_USER}:${NB_USER} pipeline-notebooks pipeline-notebooks

EXPOSE 5000

ENTRYPOINT ["uvicorn", "prepline_general.api.app:app", \
  "--host", "0.0.0.0"]

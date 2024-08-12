# syntax=docker/dockerfile:experimental
FROM harbor.sionic.tech/usio-test/2b83f193aabb AS base

USER root

# Set up environment
ENV PYTHON=python3.11
ENV PIP="${PYTHON} -m pip"
ENV HOME=/root
ENV PIPELINE_PACKAGE=general

WORKDIR ${HOME}

# Update PYTHONPATH
ENV PYTHONPATH="/app:${PYTHONPATH}"
ENV PATH="${HOME}/.local/bin:${PATH}"

FROM base as python-deps
COPY requirements/base.txt requirements-base.txt
RUN ${PIP} install pip==23.2.1
RUN ${PIP} install --no-cache-dir -r requirements-base.txt

FROM python-deps as model-deps
# Create a directory for the unstructured package
RUN mkdir -p /app/unstructured

# Create symbolic link for unstructured if necessary
RUN if [ ! -d "$(${PYTHON} -c "import site; print(site.getsitepackages()[0])")/unstructured" ]; then \
        ln -s /app/unstructured $(${PYTHON} -c "import site; print(site.getsitepackages()[0])")/unstructured; \
    fi

# Reset Python environment
RUN ${PYTHON} -m site

# Debug information
RUN echo "PYTHONPATH: $PYTHONPATH" && \
    ${PYTHON} -c "import sys; print('Python sys.path:', sys.path)" && \
    ls -l /app && \
    ${PYTHON} --version && \
    ${PYTHON} -c "import site; print(site.getsitepackages())" && \
    ${PYTHON} -c "import unstructured; print(unstructured.__file__)" || echo "Failed to import unstructured"

# Download NLTK data and initialize unstructured
RUN ${PYTHON} -c "import nltk; nltk.download('punkt')" && \
    ${PYTHON} -c "import nltk; nltk.download('averaged_perceptron_tagger')" && \
    ${PYTHON} -c "from unstructured.partition.model_init import initialize; initialize()"

FROM model-deps as code
# COPY CHANGELOG.md CHANGELOG.md
COPY logger_config.yaml logger_config.yaml
COPY prepline_general/ prepline_general/
# COPY exploration-notebooks exploration-notebooks
COPY scripts/app-start.sh scripts/app-start.sh

ENTRYPOINT ["scripts/app-start.sh"]
# Expose a default port of 8000
EXPOSE 8000
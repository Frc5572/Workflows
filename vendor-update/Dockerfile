FROM python:3.13-alpine

WORKDIR /app
COPY pr-template.j2 requirements.txt vendor-update.py /app/
RUN python -m pip install -U pip setuptools && pip install -r requirements.txt
ENTRYPOINT [ "python" ]
CMD [ "vendor-update.py" ]
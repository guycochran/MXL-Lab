# Builds on the existing lab image, so GStreamer, the NDI plugin and
# mxl-info are already present. Only the web stack is added.
FROM mxl-lab:latest

# Pinned to the versions the GUI was tested with
RUN pip3 install --break-system-packages --no-cache-dir \
      "fastapi==0.141.1" "uvicorn[standard]==0.54.0" "docker==7.2.0"

COPY app.py /opt/gui/app.py
COPY lab/ /opt/gui/lab/
COPY static/ /opt/gui/static/

WORKDIR /opt/gui
CMD ["sh", "-c", "exec uvicorn app:app --host ${GUI_BIND:-0.0.0.0} --port ${GUI_PORT:-8080}"]

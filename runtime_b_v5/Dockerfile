# Runtime B V5 — packaged for an AWS Lambda MicroVM image.
#
# The Lambda-managed MicroVM base image (the OS + service components) is
# specified separately at build time via --base-image-arn; this Dockerfile
# defines the application layers (container base image).
FROM public.ecr.aws/lambda/microvms:al2023-minimal

# Install awscli via pip (not dnf): the dnf awscli on this base ships a
# second copy under /usr/bin that cannot see pip-installed deps like
# python-dateutil, causing "ModuleNotFoundError: No module named 'dateutil'".
# google-noto-sans-cjk: Chinese fonts so matplotlib chart labels render (al2023-minimal has none).
RUN dnf install -y python3 python3-pip google-noto-sans-cjk-ttc-fonts && dnf clean all

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir awscli -r requirements.txt

COPY main.py .

ENV PYTHONUNBUFFERED=1 APP_PORT=8080 HOOK_PORT=9000 WORKSPACE_DIR=/tmp/workspace

# 8080 = app traffic (shell/python), 9000 = lifecycle hooks
EXPOSE 8080 9000

# Lambda starts the app, waits for the /ready hook (HTTP 200), then snapshots.
CMD ["python3", "main.py"]

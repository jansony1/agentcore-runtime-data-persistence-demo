# Sample Sandbox Runtime — MicroVM image for the MySQL→analysis demo.
# Bakes in pandas/numpy/matplotlib + pymysql + CJK fonts so the model's Python
# can connect to RDS MySQL and chart in Chinese with no runtime install.
FROM public.ecr.aws/lambda/microvms:al2023-minimal

RUN dnf install -y python3 python3-pip google-noto-sans-cjk-ttc-fonts && dnf clean all

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY main.py .

ENV PYTHONUNBUFFERED=1 APP_PORT=8080 HOOK_PORT=9000 WORKSPACE_DIR=/tmp/workspace
EXPOSE 8080 9000
CMD ["python3", "main.py"]

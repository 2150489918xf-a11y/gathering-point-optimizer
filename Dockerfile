FROM python:3.12-slim

# 中文字体：否则 matplotlib 画出的中文全是方块。
# Debian 官方源在阿里云等环境常超时，换清华源。
RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g; \
            s|http://security.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

# 国内网络下 PyPI 官方源经常读超时，换清华镜像源并放宽超时。
# --timeout 600 是关键：官方源15 秒默认超时在慢速下载时必崩。
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
        --trusted-host pypi.tuna.tsinghua.edu.cn \
        --timeout 600 --retries 5 \
        numpy \
        pandas \
        scipy \
        requests \
        matplotlib \
        pulp \
        scikit-learn \
        spopt \
        osmnx \
        networkx \
        rasterio

WORKDIR /work
CMD ["python", "calc_points.py"]
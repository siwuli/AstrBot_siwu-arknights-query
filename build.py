# -*- coding: UTF-8 -*-
"""
打包脚本：把 siwu-arknights-query 插件源码打包为 zip，用于 AstrBot 网页端上传安装。

版本号自动读取自 metadata.yaml 中的 version，输出 dist/siwu-arknights-query-<version>.zip
（dist 位于本插件的上级目录，即 plugins/astrbot/dist/）。
zip 内文件直接放在根目录（main.py / metadata.yaml / _conf_schema.json / requirements.txt 等），
AstrBot 上传安装时会自动校验 metadata.yaml 并解压到 data/plugins/<name>，依赖由
requirements.txt 自动安装，无需捆绑第三方包。

用法：
    python plugins/astrbot/siwu-arknights-query-1_0/build.py
"""

import os
import re
import zipfile

import yaml

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ASTRBOT_DIR = os.path.dirname(PLUGIN_DIR)  # plugins/astrbot
PLUGIN_SLUG = 'siwu-arknights-query'

# 打包时排除的文件/目录
EXCLUDE_NAMES = {
    os.path.basename(__file__),  # build.py 自身
    '__pycache__',
    '.git',
    '.gitignore',
}


def plugin_version() -> str:
    """从 metadata.yaml 读取插件版本号（version: x.y.z）"""
    with open(os.path.join(PLUGIN_DIR, 'metadata.yaml'), encoding='utf-8') as f:
        metadata = yaml.safe_load(f)
    version = (metadata or {}).get('version', '')
    m = re.search(r'(\d+\.\d+\.\d+)', str(version))
    return m.group(1) if m else '1.0.0'


def output_zip() -> str:
    return os.path.join(ASTRBOT_DIR, 'dist', f'{PLUGIN_SLUG}-{plugin_version()}.zip')


def _collect_files(base: str) -> list[str]:
    """递归收集插件目录下需要打包的文件（相对路径），排除 EXCLUDE_NAMES。"""
    files = []
    for root, dirs, names in os.walk(base):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_NAMES]
        for name in names:
            if name.endswith(('.pyc', '.pyo')) or name in EXCLUDE_NAMES:
                continue
            files.append(os.path.relpath(os.path.join(root, name), base))
    return sorted(files)


def build() -> str:
    output = output_zip()
    os.makedirs(os.path.dirname(output), exist_ok=True)

    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as zf:
        for rel in _collect_files(PLUGIN_DIR):
            arcname = rel.replace(os.sep, '/')  # zip 内统一用正斜杠，文件放根目录
            print(f'  + {arcname}')
            zf.write(os.path.join(PLUGIN_DIR, rel), arcname=arcname)

    print(f'\ncreated: {output} ({os.path.getsize(output)} bytes)')
    return output


if __name__ == '__main__':
    build()

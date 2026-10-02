# 发版流程

## 日常：改版本号 → 打 tag → 推送

```shell
# 1) 改 pyproject.toml 里的 version（PyPI 上已发布的版本号不能覆盖）
# 2) 提交
git add -A
git commit -m "发布 v0.2.1"
# 3) 打 tag 并推
git tag v0.2.1
git push origin master --tags
```

推 tag 之后 [`.github/workflows/release.yml`](../.github/workflows/release.yml) 自动做四件事：

1. `uv build --wheel` 出 wheel；
2. `uvx twine check` 校验元数据与 README；
3. 建 **GitHub Release**，自动生成发布说明，并把 wheel 作为附件挂上；
4. 发到 **PyPI**（版本已存在会自动跳过，不报错）。

（只发 wheel，不发 sdist，原因见文末；GitHub Packages 也不支持 Python 包。）

平时往 `master` 推代码或提 PR 会跑 [`ci.yml`](../.github/workflows/ci.yml)：
语法检查、裸跑自检、打包校验，再把 wheel 装进干净 venv 跑一遍 `patch-browser`。

## 一次性配置

### PyPI：Trusted Publisher（推荐，仓库里不用存 token）

**本项目已经配好了（2026-10-02）**，下面只是记录和备用步骤。

入口是**项目设置**里的 Publishing（不是账号页那个 "pending publisher"，那是给还不存在的项目用的）：

```
https://pypi.org/manage/project/patch-browser/settings/publishing/
→ Add a new publisher → GitHub
```

| 字段 | 值 |
| --- | --- |
| Owner | `DreamXiaoJing` |
| Repository name | `debug-default-profile` |
| Workflow name | `release.yml` |
| Environment name | 留空（显示为 `(Any)`，对应 workflow 里没有 `environment:`） |

配好之后，release.yml 里的 `pypa/gh-action-pypi-publish` 直接用 OIDC 发布，**不需要任何 secret**。

要是哪天配错了或者想撤销：同一个页面点该 publisher 的 **Remove**。没配也不致命——那一步会失败，
但它开了 `continue-on-error`，不影响 GitHub Release。

### 用 token 发布（不想用 OIDC 时）

仓库 → Settings → Secrets and variables → Actions → New repository secret，
名字 `PYPI_API_TOKEN`，值填 PyPI 的**项目级** token；然后把 release.yml 里「发布到 PyPI」那步替换成：

```yaml
      - name: 发布到 PyPI
        continue-on-error: true
        env:
          TWINE_USERNAME: __token__
          TWINE_PASSWORD: ${{ secrets.PYPI_API_TOKEN }}
        run: uvx twine upload --skip-existing dist/*
```

### 关于 GitHub Packages（仓库侧边栏那个面板）

**不用配，也配不了**：GitHub Packages 没有 Python / PyPI 仓库——官方支持的只有
npm、RubyGems、Maven、Gradle、NuGet 和容器镜像（见 GitHub 文档的
Supported clients and formats）。所以仓库侧边栏的「Packages」对 Python 包一直是空的，
这是正常的，Python 包统一走 PyPI。

真想让 Packages 面板有东西，只能发**容器镜像**（ghcr.io）；但这个工具是 Windows 专属
（要写 `Program Files` 下的 dll、用 `tasklist`），做成容器没有意义，所以不做。

> 早期版本的 release.yml 里曾有一句 `twine upload --repository-url https://pypi.pkg.github.com/...`，
> 那个地址不存在（`pypi.pkg.github.com` 一律 404），已删除。

## 本机应急手动发布

```shell
uv build --wheel
uvx twine check dist/*.whl
uvx twine upload dist/*.whl      # 用户名填 __token__，密码填 PyPI token
```

## 为什么只发 wheel、不发 sdist

wheel 里**只有该有的东西**：

```
patch_browser.py                 # 模块本身
patch_db.default.json            # 出厂参数（首次运行复制到用户目录）
patch_browser-0.2.0.dist-info/   # 元数据 + LICENSE
```

而 sdist 里 hatchling 会按 VCS 文件列表把项目杂物一起打进去——`include`、`only-include`、
`exclude`、`ignore-vcs` 四种配置都压不住 `.gitignore`，之前还漏进过 `.idea/`、
`.workbuddy/`（本地笔记）、`uv.lock`、`一键修复.bat`。与其一个个往外挑，不如干脆不发 sdist：
这个包是纯 Python 的 `py3-none-any` wheel，任何平台都能装；要源码的人用 GitHub Release
自带的 “Source code” 归档，或者直接 clone。

## 注意

- **PyPI 上已发布的版本号不能覆盖**：改了内容必须升版本号（`0.2.0` → `0.2.1`）。
- token 只用于手动发布，**用完就去 PyPI 吊销**；能走 OIDC 就别存 token。
- **发行物只放该放的**：这里只发 wheel，`pyproject.toml` 里没有 sdist 构建；
  真要往 wheel 里加东西，只动 `[tool.hatch.build.targets.wheel]` 的
  `only-include` / `force-include` 两个白名单字段。
- 发版前确认 `README.md` 与 `README.en.md` 章节同步——`readme` 会作为 PyPI 项目页正文。

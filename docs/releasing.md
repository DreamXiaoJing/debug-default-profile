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

1. `uv build` 出 wheel + sdist；
2. `uvx twine check` 校验元数据与 README；
3. 建 **GitHub Release**，自动生成发布说明，并把 `dist/*` 作为附件挂上；
4. 发到 **PyPI** 和 **GitHub Packages**（版本已存在会自动跳过，不报错）。

平时往 `master` 推代码或提 PR 会跑 [`ci.yml`](../.github/workflows/ci.yml)：
语法检查、裸跑自检、打包校验，再把 wheel 装进干净 venv 跑一遍 `patch-browser`。

## 一次性配置

### PyPI：Trusted Publisher（推荐，仓库里不用存 token）

pypi.org → 项目 `patch-browser` → **Manage → Publishing → Add a new pending publisher**：

| 字段 | 值 |
| --- | --- |
| PyPI Project Name | `patch-browser` |
| Owner | `DreamXiaoJing` |
| Repository name | `debug-default-profile` |
| Workflow name | `release.yml` |
| Environment name | 留空 |

配好之后，release.yml 里的 `pypa/gh-action-pypi-publish` 直接用 OIDC 发布，**不需要任何 secret**。

没配也行：那一步会失败，但它开了 `continue-on-error`，不影响 GitHub Release 和 GitHub Packages。

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

### GitHub Packages

用仓库自带的 `GITHUB_TOKEN`，workflow 里已经开了 `packages: write`，**不需要额外配置**。

## 本机应急手动发布

```shell
uv build
uvx twine check dist/*
uvx twine upload dist/*      # 用户名填 __token__，密码填 PyPI token
```

## 注意

- **PyPI 上已发布的版本号不能覆盖**：改了内容必须升版本号（`0.2.0` → `0.2.1`）。
- token 只用于手动发布，**用完就去 PyPI 吊销**；能走 OIDC 就别存 token。
- sdist 的排除规则在 `pyproject.toml` 的 `[tool.hatch.build.targets.sdist]`：
  `.idea` / `.workbuddy` 这类本地文件不要打进去（曾经把本地笔记打进 sdist，上传前才发现）。
- 发版前确认 `README.md` 与 `README.en.md` 章节同步——`readme` 会作为 PyPI 项目页正文。

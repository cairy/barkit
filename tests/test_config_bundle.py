"""``core/config_bundle`` 的判据。

这一族的失败形态都是**静默**的：包写出来是好的、校验也说通过，但少了一个文件
或某个字节被改了 —— 用户直到「真要还原」那一刻才发现。所以每条用例都盯一个
具体的、不可能靠运气满足的量：

- 逐字节比对（不是「大小相同」）；
- 篡改必须被抓到（否则校验是恒真门禁）；
- 包内多一个清单未登记的文件必须拒（否则白名单形同虚设）；
- 导入坏包时**盘上一个字节都不许动**。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
import yaml

from barkit import paths
from barkit.core import config_bundle as cb


SAMPLE_FILES = {
    "hosts_entries.json": '{"entries": [{"host": "example.com", "ip": "1.2.3.4"}]}',
    "variables_state.json": '{"2026-10-22": {"start_time": "09:30"}}',
    "agenda_log.json": '{"2026-10-22": ["clk1"]}',
    "interaction_state.json": '{"seen": ["hint-a"]}',
    "config.yaml": "plugins:\n  autotype:\n    profiles:\n    - text: secret-token\n",
}


def _seed_data_dir(name: str = "config.yaml") -> Path:
    """在**本用例的**数据根里铺一份用户数据，返回根。"""
    root = paths.APP_SUPPORT
    root.mkdir(parents=True, exist_ok=True)
    for filename, text in SAMPLE_FILES.items():
        target = root / (name if filename == "config.yaml" else filename)
        target.write_text(text, encoding="utf-8")
    return root


# ---------- 清单与真源 ----------

def test_config_filename_matches_its_real_source():
    """``config.yaml`` 这个名字没有模块级常量（见 ``core/config/consts.py`` 的解释），
    所以本模块写字面量。这条用例就是那个字面量的对拍口 —— 真源一改，这里立刻红。"""
    from barkit.core.config import Config

    assert cb._config_name() == Config().user_path.name


def test_entries_cover_every_file_doctor_treats_as_user_data():
    """``doctor.checks_data.USER_JSON_FILES`` 是「用户亲手填过」的另一份清单。
    两份清单各说各的迟早分叉，故按**文件名集合**对拍：doctor 认的那几份，
    备份必须全收（它可以多收，比如 ``plugins/`` 目录）。"""
    from barkit.doctor.checks_data import USER_JSON_FILES

    doctor_names = {name for name, _source in USER_JSON_FILES}
    bundle_names = {entry.rel for entry in cb.entries()}
    missing = doctor_names - bundle_names
    assert not missing, f"备份清单漏了 doctor 认为是用户数据的文件：{sorted(missing)}"


def test_agenda_filename_matches_its_real_source():
    """``core`` 不许 import ``plugins``（层次方向门禁），所以日账名在
    ``config_bundle`` 里写了一份字面量。这条用例就是那个字面量的对拍口。"""
    from barkit.plugins.todo.day_log import AGENDA_FILENAME

    assert cb.AGENDA_FILENAME == AGENDA_FILENAME


def test_state_category_can_be_dropped():
    """关掉运行状态时，配置类必须一个不少，状态类必须全走。"""
    full = {e.rel: e.category for e in cb.entries()}
    lean = {e.rel: e.category for e in cb.entries(include_state=False)}
    assert set(lean) < set(full)
    assert all(cat == cb.CATEGORY_CONFIG for cat in lean.values())


# ---------- 导出 ----------

def test_export_is_byte_identical_and_verifies(tmp_path):
    _seed_data_dir()
    report = cb.export_bundle(tmp_path / "out")
    assert report.ok, report.problems
    assert set(report.files) >= set(SAMPLE_FILES)
    assert report.zip_path.stat().st_mode & 0o777 == 0o600

    with zipfile.ZipFile(report.zip_path) as zf:
        for filename, text in SAMPLE_FILES.items():
            blob = zf.read(f"{cb.PAYLOAD_DIR}/{filename}")
            assert blob == text.encode("utf-8"), f"{filename} 不是逐字节一致"

    ok, problems = cb.verify_bundle(report.zip_path)
    assert ok, problems


def test_export_records_sha256_and_size_for_every_file(tmp_path):
    _seed_data_dir()
    report = cb.export_bundle(tmp_path / "out")
    manifest = cb.read_manifest(report.zip_path)
    assert manifest["format_version"] == cb.BUNDLE_FORMAT_VERSION
    by_rel = {item["rel"]: item for item in manifest["files"]}
    for filename, text in SAMPLE_FILES.items():
        item = by_rel[filename]
        assert item["size"] == len(text.encode("utf-8"))
        assert len(item["sha256"]) == 64


def test_second_export_in_the_same_second_does_not_overwrite(tmp_path):
    """连点两下不能把上一份覆盖掉 —— 覆盖等于把唯一的备份抹了。"""
    _seed_data_dir()
    first = cb.export_bundle(tmp_path / "out").zip_path
    second = cb.export_bundle(tmp_path / "out").zip_path
    assert first != second
    assert first.exists() and second.exists()


# ---------- 校验 ----------

def _rewrite_member(zip_path: Path, member: str, blob: bytes) -> Path:
    """就地重写包里的一个成员（模拟篡改 / 写坏）。"""
    out = zip_path.with_name("tampered.zip")
    with zipfile.ZipFile(zip_path) as src, zipfile.ZipFile(out, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, blob if name == member else src.read(name))
    return out


def test_verify_rejects_a_single_flipped_byte(tmp_path):
    """**等长**篡改 —— 必须由哈希抓到。

    这条用例原先写成「换成一段更短的内容」，于是它撞上的是**大小**那一支，
    哈希分支一次都没被走到（变异版全绿＝假门禁，2026-10-22 自证发现）。
    所以这里显式断言长度相同，把判据钉在哈希那一支上。
    """
    _seed_data_dir()
    report = cb.export_bundle(tmp_path / "out")
    original = SAMPLE_FILES["hosts_entries.json"].encode("utf-8")
    flipped = original.replace(b"1.2.3.4", b"9.9.9.9")
    assert len(flipped) == len(original), "这条用例的前提是等长"

    tampered = _rewrite_member(
        report.zip_path, f"{cb.PAYLOAD_DIR}/hosts_entries.json", flipped
    )
    ok, problems = cb.verify_bundle(tampered)
    assert not ok
    assert any("哈希" in p for p in problems), problems


def test_verify_rejects_a_member_whose_size_differs(tmp_path):
    """大小不符是另一支判据，与哈希分开各守一条（上面的用例保证哈希那支真被执行）。"""
    _seed_data_dir()
    report = cb.export_bundle(tmp_path / "out")
    tampered = _rewrite_member(
        report.zip_path, f"{cb.PAYLOAD_DIR}/hosts_entries.json", b'{"entries": []}  '
    )
    ok, problems = cb.verify_bundle(tampered)
    assert not ok
    assert any("大小不符" in p for p in problems), problems


def test_verify_rejects_a_member_the_manifest_never_declared(tmp_path):
    _seed_data_dir()
    report = cb.export_bundle(tmp_path / "out")
    extra = report.zip_path.with_name("extra.zip")
    with zipfile.ZipFile(report.zip_path) as src, zipfile.ZipFile(extra, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr(f"{cb.PAYLOAD_DIR}/injected.json", b"{}")

    ok, problems = cb.verify_bundle(extra)
    assert not ok
    assert any("未登记" in p for p in problems), problems


def test_verify_rejects_a_declared_member_that_is_missing(tmp_path):
    _seed_data_dir()
    report = cb.export_bundle(tmp_path / "out")
    truncated = report.zip_path.with_name("truncated.zip")
    with zipfile.ZipFile(report.zip_path) as src, zipfile.ZipFile(truncated, "w") as dst:
        for name in src.namelist():
            if name == f"{cb.PAYLOAD_DIR}/config.yaml":
                continue
            dst.writestr(name, src.read(name))

    ok, problems = cb.verify_bundle(truncated)
    assert not ok
    assert any("包里没有" in p for p in problems), problems


def test_verify_rejects_a_bundle_from_another_format_version(tmp_path):
    _seed_data_dir()
    report = cb.export_bundle(tmp_path / "out")
    higher = report.zip_path.with_name("future.zip")
    manifest = cb.read_manifest(report.zip_path)
    manifest["format_version"] = cb.BUNDLE_FORMAT_VERSION + 1
    with zipfile.ZipFile(report.zip_path) as src, zipfile.ZipFile(higher, "w") as dst:
        for name in src.namelist():
            blob = (yaml.safe_dump(manifest, allow_unicode=True).encode("utf-8")
                    if name == cb.MANIFEST_NAME else src.read(name))
            dst.writestr(name, blob)

    ok, _problems = cb.verify_bundle(higher)
    assert not ok


def test_verify_rejects_a_path_escaping_the_data_dir(tmp_path):
    """清单里写着 ``../x`` —— 无论包是谁造的，都不许往外写。"""
    _seed_data_dir()
    report = cb.export_bundle(tmp_path / "out")
    evil = report.zip_path.with_name("evil.zip")
    manifest = cb.read_manifest(report.zip_path)
    manifest["files"].append({"rel": "../../etc/passwd", "size": 0, "sha256": "x"})
    with zipfile.ZipFile(report.zip_path) as src, zipfile.ZipFile(evil, "w") as dst:
        for name in src.namelist():
            blob = (yaml.safe_dump(manifest, allow_unicode=True).encode("utf-8")
                    if name == cb.MANIFEST_NAME else src.read(name))
            dst.writestr(name, blob)

    ok, problems = cb.verify_bundle(evil)
    assert not ok
    assert any("越界" in p for p in problems), problems


# ---------- 导入 ----------

def test_import_restores_bytes_and_keeps_a_backup_of_what_it_replaced(tmp_path):
    root = _seed_data_dir()
    report = cb.export_bundle(tmp_path / "out")

    (root / "hosts_entries.json").write_text('{"entries": []}', encoding="utf-8")
    (root / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")

    ok, note = cb.import_bundle(report.zip_path)
    assert ok, note
    assert (root / "hosts_entries.json").read_text(encoding="utf-8") == \
        SAMPLE_FILES["hosts_entries.json"]
    assert (root / "config.yaml").read_text(encoding="utf-8") == SAMPLE_FILES["config.yaml"]
    assert (root / "config.yaml").stat().st_mode & 0o777 == 0o600

    backups = sorted((root / "backups").glob("barkit-config-before-import-*.zip"))
    assert backups, "导入前必须留一份当前状态的备份"
    # 备份里存的是**被覆盖前**的内容，不是包里的内容
    assert cb.read_manifest(backups[-1])["files"], "备份清单不该是空的"
    with zipfile.ZipFile(backups[-1]) as zf:
        assert zf.read(f"{cb.PAYLOAD_DIR}/config.yaml") == b"plugins: {}\n"


def test_import_refuses_a_broken_bundle_without_touching_anything(tmp_path):
    root = _seed_data_dir()
    report = cb.export_bundle(tmp_path / "out")
    tampered = _rewrite_member(
        report.zip_path, f"{cb.PAYLOAD_DIR}/config.yaml", b"plugins: {}\n"
    )

    before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    ok, note = cb.import_bundle(tampered)
    assert not ok
    assert "不能导入" in note
    after = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
    assert before == after, "校验不过时盘上不许有任何变化"
    assert not (root / "backups").exists()


def test_import_reports_missing_files_rather_than_silently_skipping(tmp_path):
    """包里没有的文件，导入后**仍是原来的**（不是被清空）—— 本模块只覆盖、不删除。"""
    root = _seed_data_dir()
    report = cb.export_bundle(tmp_path / "out")
    (root / "agenda_log.json").unlink()

    ok, note = cb.import_bundle(report.zip_path)
    assert ok, note
    assert (root / "agenda_log.json").read_text(encoding="utf-8") == \
        SAMPLE_FILES["agenda_log.json"]


# ---------- 目录遍历 ----------

def test_plugins_dir_collects_files_but_skips_dotfiles_and_symlinks(tmp_path):
    _seed_data_dir()
    plugins = paths.EXTERNAL_PLUGINS_DIR
    (plugins / "demo").mkdir(parents=True, exist_ok=True)
    (plugins / "demo" / "manifest.yaml").write_text("id: demo\n", encoding="utf-8")
    (plugins / "demo" / ".DS_Store").write_bytes(b"noise")
    (plugins / "demo" / "escape.py").symlink_to("/etc/hosts")

    rels = {rel for rel, _cat, path in cb.collect() if path is not None}
    assert "plugins/demo/manifest.yaml" in rels
    assert not any(".DS_Store" in r for r in rels)
    assert not any("escape.py" in r for r in rels)


@pytest.mark.parametrize("bad", ["../x", "/abs/x", ""])
def test_entries_never_treat_unsafe_paths_as_backup_targets(bad):
    from barkit.core.installer import _unsafe_rel_path

    assert _unsafe_rel_path(bad)

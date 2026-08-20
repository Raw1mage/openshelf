"""BR-223000 迴歸鎖：容器掛載邊界必須與程式碼的路徑解析一致。

這個檔鎖住的不是「掛載存在」，而是**兩者一致**——因為 BR-223000 的失效形狀是
「程式碼往一個沒被掛載的路徑寫，而 mkdir(exist_ok=True) 讓它靜默成功」。
單獨斷言其中一邊都會留下同一個漏洞：

  - 只斷言 compose 有掛載   → 有人改了 validator 的落點，掛載還在但指向別處
  - 只斷言 code 的解析路徑  → 有人拿掉掛載，解析仍然正確但寫進 ephemeral 目錄

兩者都對、且指向同一個位置，才是 BR-223000 真正要的保證。
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE = REPO_ROOT / "docker-compose.yml"

# 容器內 package 落點（compose 的 ./app:/app/app）。validator.py 在容器內是
# /app/app/crawler/validator.py，往上三層 = /app，故 issues_dir = /app/issues。
CONTAINER_PACKAGE_PARENT = "/app"


def _volumes() -> list[str]:
    """取出 compose 的 volumes 清單（純文字解析，不引入 yaml 相依）。"""
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped == "volumes:":
            inside = True
            continue
        if inside:
            if stripped.startswith("- "):
                out.append(stripped[2:].strip())
            elif stripped and not stripped.startswith("#"):
                break
    return out


def test_compose_parser_actually_reads_volumes():
    """控制組：先證明這支解析器讀得到東西。

    沒有這一條，下面兩個測試在「解析器壞掉回空 list」時會以最令人安心的方式失敗
    ——或更糟，若某天改成 `any(...)` 形式的斷言，空 list 會讓它靜默通過。
    """
    vols = _volumes()
    assert vols, "compose volumes 解析為空——解析器壞了，不是 compose 沒有掛載"
    assert any(v.endswith(":/app/app") for v in vols), (
        f"連 ./app:/app/app 這條一定存在的掛載都沒解析到，解析器不可信：{vols}"
    )


def test_issues_dir_is_mounted_not_ephemeral():
    """BR-223000 主鎖：issues 目錄必須被掛載進容器。

    缺這條掛載時，MirrorValidator.__init__ 的 mkdir(parents=True, exist_ok=True)
    會在容器內造出一個 ephemeral 目錄，dispatch_br 寫進去的診斷 BR 於 rebuild
    時全部消失，而前端「已派發 BR」清單恆為空——**零錯誤、零 log**。
    """
    vols = _volumes()
    expected = f"./issues:{CONTAINER_PACKAGE_PARENT}/issues"
    assert expected in vols, (
        f"docker-compose.yml 缺少 {expected!r} 掛載。\n"
        f"現有 volumes：{vols}\n"
        "沒有它，容器產生的診斷 BR 會寫進 rebuild 即消失的目錄，且不會有任何錯誤訊號。"
    )


def test_container_mount_target_matches_code_resolution():
    """掛載目標必須等於程式碼在容器內解析出來的路徑。

    validator.py 用 `Path(__file__).parent.parent.parent / "issues"` 解析落點。
    容器內 __file__ = /app/app/crawler/validator.py，往上三層 = /app。
    若有人改了那個相對層數、或改了 ./app 的掛載目標，這條會炸——
    而缺了它，掛載可以「存在但指向沒人寫入的地方」，症狀與完全沒掛載完全相同。
    """
    validator = REPO_ROOT / "app" / "crawler" / "validator.py"
    src = validator.read_text(encoding="utf-8")

    # 落點的解析方式若改變，這個鎖的前提就不成立，必須同步檢討而不是靜默放行。
    anchor = 'Path(__file__).parent.parent.parent / "issues"'
    assert anchor in src, (
        f"validator.py 的 issues_dir 解析方式已改變（找不到 {anchor!r}）。\n"
        "本測試的前提（容器內解析為 /app/issues）可能不再成立，請重新檢視 BR-223000。"
    )

    vols = _volumes()
    package_mounts = [v for v in vols if v.endswith(":/app/app")]
    assert package_mounts == ["./app:/app/app"], (
        f"package 掛載已改變：{package_mounts}。"
        f"容器內 issues_dir 的解析結果會跟著變，{CONTAINER_PACKAGE_PARENT}/issues 不再正確。"
    )


@pytest.mark.parametrize("bogus", ["./zzz_no_such:/app/zzz_no_such"])
def test_absent_mount_is_detectable(bogus):
    """控制組：證明上面的斷言在該失敗時真的會失敗。

    若 `in vols` 這個判斷因為某種原因恆為真（例如 _volumes() 回傳了包含一切的東西），
    上面兩條測試就變成永遠通過的裝飾品。這條用一個確定不存在的掛載反向證明它有鑑別力。
    """
    assert bogus not in _volumes(), (
        "一個確定不存在的掛載竟然被判定為存在——`in vols` 的判斷沒有鑑別力，"
        "上面的測試全部不可信。"
    )

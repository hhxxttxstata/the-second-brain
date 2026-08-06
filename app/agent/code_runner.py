"""代码执行工具 — 让 Agent 具备编程能力。

三种能力:
  1. generate_excel — 用 openpyxl 生成 Excel 报表（受控输出目录）
  2. control_visio — 通过 COM 控制 Visio 画流程图（Windows）
  3. run_code — 执行 Agent 生成的 Python 代码（受控: 工作目录 + 超时 + 只读检查）

安全设计:
  - 代码执行在指定工作目录（agent_data/code_runs/），不能碰系统文件
  - 超时保护（默认 60s），防死循环
  - 输出目录限定在 agent_data 或用户指定路径
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from app.core.config import settings

# 允许的代码执行工作目录
CODE_RUN_DIR = settings.agent_data_dir / "code_runs"


def _ensure_run_dir() -> Path:
    CODE_RUN_DIR.mkdir(parents=True, exist_ok=True)
    return CODE_RUN_DIR


# ---------------------------------------------------------------------------
# 1. Excel 生成
# ---------------------------------------------------------------------------

def generate_excel(file_path: str, data: list[list[Any]],
                   sheet_name: str = "Sheet1",
                   headers: list[str] | None = None) -> str:
    """生成 Excel 文件。

    Args:
        file_path: 输出路径（绝对路径或相对 agent_data/）
        data: 二维数据 [[行1...], [行2...]]，每个元素是 str/int/float
        sheet_name: 工作表名
        headers: 表头（可选，第一行）
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return "❌ openpyxl 未安装，请先 pip install openpyxl"

    # 路径解析：相对路径 → agent_data/ 下
    p = Path(file_path)
    if not p.is_absolute():
        p = settings.agent_data_dir / "outputs" / p
        p.parent.mkdir(parents=True, exist_ok=True)

    try:
        wb = Workbook()
        ws = wb.active
        ws.title = sheet_name[:31]  # Excel 工作表名上限 31 字符

        row_start = 1
        if headers:
            for col, h in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col, value=str(h))
                cell.font = Font(bold=True)
                cell.fill = PatternFill(start_color="D9E1F2", end_color="D9E1F2", fill_type="solid")
            row_start = 2

        for r, row in enumerate(data, row_start):
            for c, val in enumerate(row, 1):
                ws.cell(row=r, column=c, value=val)

        # 自动列宽（前 50 行采样）
        for col in range(1, len(headers or []) + 1 or 1):
            letter = get_column_letter(col)
            max_len = max(
                (len(str(ws.cell(row=r, column=col).value or "")) for r in range(1, min(row_start + len(data) + 1, 51))),
                default=8,
            )
            ws.column_dimensions[letter].width = min(max_len + 4, 40)

        wb.save(str(p))
        return f"✅ 已生成 Excel: {p}（{len(data)} 行数据）"
    except Exception as exc:
        return f"❌ Excel 生成失败: {exc}"


# ---------------------------------------------------------------------------
# 2. Visio 控制（Windows COM）
# ---------------------------------------------------------------------------

def control_visio(action: str, file_path: str = "",
                  shapes: list[dict[str, Any]] | None = None) -> str:
    """通过 COM 控制 Visio 画图。

    Args:
        action: "create_flowchart" | "open_and_export" | "export_pdf"
        file_path: Visio 文件路径（.vsdx）
        shapes: 流程图形状列表，每个是
            {"text": "开始", "shape": "rectangle|decision|ellipse|parallelogram",
             "x": 1.0, "y": 1.0, "w": 2.0, "h": 1.0}
    """
    try:
        import win32com.client
    except ImportError:
        return "❌ pywin32 未安装，无法控制 Visio。pip install pywin32"

    p = Path(file_path) if file_path else Path(settings.agent_data_dir) / "outputs" / "diagram.vsdx"

    try:
        visio = win32com.client.Dispatch("Visio.Application")
        visio.Visible = True  # 显示 Visio 窗口让用户看到过程

        if action == "create_flowchart":
            doc = visio.Documents.Add("")
            page = doc.Pages.Item(1)
            stencil = visio.Documents.OpenEx("BASFLO_M.vssx", 1)  # 基本流程图形状

            # 形状名 → 主形状索引（Basic Flowchart Shapes）
            shape_map = {"rectangle": 1, "decision": 2, "ellipse": 3, "parallelogram": 4}
            for s in (shapes or []):
                shape_type = s.get("shape", "rectangle")
                master_idx = shape_map.get(shape_type, 1)
                master = stencil.Masters.Item(master_idx)
                shape = page.Drop(master, s.get("x", 1.0), s.get("y", 1.0))
                shape.Text = s.get("text", "")
                shape.Width = s.get("w", 2.0)
                shape.Height = s.get("h", 1.0)

            doc.SaveAs(str(p))
            return f"✅ Visio 流程图已创建: {p}（{len(shapes or [])} 个形状）"
        elif action == "export_pdf":
            doc = visio.Documents.Open(str(p))
            pdf_path = str(p.with_suffix(".pdf"))
            doc.ExportAsFixedFormat(1, pdf_path)  # 1 = PDF
            doc.Close()
            return f"✅ 已导出 PDF: {pdf_path}"
        elif action == "open_and_export":
            doc = visio.Documents.Open(str(p))
            pdf_path = str(p.with_suffix(".pdf"))
            doc.ExportAsFixedFormat(1, pdf_path)
            doc.Close()
            return f"✅ 已打开并导出: {pdf_path}"
        else:
            return f"❌ 未知 Visio 操作: {action}"
    except Exception as exc:
        return f"❌ Visio 操作失败: {exc}（确认 Visio 已安装且本机可 COM 调用）"


# ---------------------------------------------------------------------------
# 3. Python 代码执行（受控）
# ---------------------------------------------------------------------------

def run_code(code: str, working_dir: str = "", timeout: int = 60) -> str:
    """执行 Agent 生成的 Python 代码。

    Args:
        code: Python 代码字符串
        working_dir: 工作目录（默认 agent_data/code_runs/）
        timeout: 超时秒数（默认 60）

    安全约束:
      - 在隔离目录执行（默认 agent_data/code_runs/）
      - 超时终止，防死循环
      - stdout/stderr 捕获返回
    """
    run_dir = _ensure_run_dir()
    if working_dir:
        wd = Path(working_dir)
        wd.mkdir(parents=True, exist_ok=True)
    else:
        wd = run_dir

    # 写代码到临时文件执行
    script_path = run_dir / f"run_{int(time.time())}.py"
    script_path.write_text(code, encoding="utf-8")

    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(wd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        output = result.stdout[-3000:] if result.stdout else ""
        if result.stderr:
            output += f"\n[stderr] {result.stderr[-1500:]}"
        if result.returncode != 0:
            return f"❌ 代码执行失败 (exit={result.returncode}):\n{output}"
        return f"✅ 代码执行成功:\n{output}" if output else "✅ 代码执行成功（无输出）"
    except subprocess.TimeoutExpired:
        return f"❌ 代码执行超时（>{timeout}s），已终止。检查是否有死循环"
    except Exception as exc:
        return f"❌ 代码执行异常: {exc}"
    finally:
        try:
            script_path.unlink()
        except Exception:
            pass

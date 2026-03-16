#!/usr/bin/env python3
"""
IEEE Xplore 论文抓取爬虫 (Playwright版本)
功能：抓取论文元数据、下载PDF
支持保存登录态
"""

import os
import re
import json
import time
import logging
from pathlib import Path
from typing import Optional, List, Dict
from playwright.sync_api import sync_playwright

from ieee_auto_login import auto_login_ieee_institution, load_ieee_credentials
from ieee_download_via_page import fetch_pdf_bytes_via_document_page

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class IEEE爬虫:
    def __init__(self, 输出目录: str = "./downloads", 保存登录态: bool = True):
        self.输出目录 = Path(输出目录)
        self.输出目录.mkdir(parents=True, exist_ok=True)
        self.保存登录态 = 保存登录态
        self.登录态文件 = self.输出目录 / "ieee_context.json"

        self.playwright = sync_playwright().start()

        # 尝试加载保存的登录态
        if self.保存登录态 and self.登录态文件.exists():
            try:
                self.browser = self.playwright.chromium.launch(
                    headless=False,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                self.context = self.browser.new_context(
                    storage_state=str(self.登录态文件),
                    viewport={"width": 1920, "height": 1080},
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                )
                logger.info("已加载保存的登录态")
            except Exception as e:
                logger.warning(f"加载登录态失败: {e}")
                self.browser = None
                self.context = None
        else:
            self.browser = None
            self.context = None

        # 如果没有登录态，启动新浏览器
        if self.context is None:
            self.browser = self.playwright.chromium.launch(
                headless=False, args=["--disable-blink-features=AutomationControlled"]
            )
            self.context = self.browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )

        self.page = self.context.new_page()
        self.page.set_default_timeout(60000)

        for i in range(3):
            try:
                self.page.goto(
                    "https://ieeexplore.ieee.org/",
                    timeout=30000,
                    wait_until="domcontentloaded",
                )
                self.page.wait_for_timeout(3000)
                break
            except Exception as e:
                logger.warning(f"访问主页失败，重试 {i + 1}/3: {e}")
                time.sleep(2)

        logger.info("浏览器已启动，请在浏览器中登录账号")
        logger.info("登录后程序会自动保存登录态")

        # 检查是否已登录
        self.检查登录()

        self.请求延迟 = (2, 5)

    def 已登录(self) -> bool:
        """基于上下文 cookie 判断当前是否存在已登录态。"""
        try:
            cookies = self.context.cookies(["https://ieeexplore.ieee.org"])
            cookie_names = {cookie.get("name", "") for cookie in cookies}
            登录cookie = {
                "xpluserinfo",
                "IEEE_ID",
                "ObSSOCookie",
                "iPlanetDirectoryPro",
            }
            if cookie_names & 登录cookie:
                return True

            logger.info(
                "当前未检测到登录 cookie，已有 cookie: %s",
                ", ".join(sorted(cookie_names)) or "(空)",
            )
        except Exception as e:
            logger.warning(f"检查登录态失败: {e}")
        return False

    def 检查登录(self):
        """检查是否已登录；如果没有则尝试自动机构登录。"""
        if self.已登录():
            logger.info("检测到已登录")
            if self.保存登录态:
                self.保存登录态到文件()
            return

        logger.info("未检测到登录态，开始自动机构登录...")
        try:
            credentials = load_ieee_credentials()
            auto_login_ieee_institution(
                self.page,
                self.context,
                credentials,
                self.登录态文件,
            )
            logger.info("自动机构登录成功")
            if self.保存登录态:
                self.保存登录态到文件()
        except Exception as e:
            logger.warning(f"自动机构登录失败，保留人工兜底: {e}")
            logger.info("请在浏览器窗口中手动登录账号...")

    def 保存登录态到文件(self):
        """保存登录态到文件"""
        try:
            self.context.storage_state(path=str(self.登录态文件))
            logger.info(f"登录态已保存到: {self.登录态文件}")
        except Exception as e:
            logger.warning(f"保存登录态失败: {e}")

    def 搜索论文(self, 关键词: str, 最大数量: int = 10) -> list:
        """搜索IEEE Xplore上的论文"""
        try:
            js_code = """
            async ({ kw, maxRows }) => {
                const response = await fetch('https://ieeexplore.ieee.org/rest/search', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json'
                    },
                    body: JSON.stringify({
                        queryText: kw,
                        pageNumber: 1,
                        rowsPerPage: maxRows,
                        returnFacets: ["ALL"],
                        returnType: "SEARCH"
                    }),
                    credentials: 'same-origin'
                });
                if (!response.ok) {
                    return {
                        __error__: `HTTP ${response.status}`,
                        __status__: response.status,
                        __text__: await response.text()
                    };
                }
                return await response.json();
            }
            """
            result = self.page.evaluate(
                js_code, {"kw": 关键词, "maxRows": max(1, 最大数量)}
            )

            if isinstance(result, dict) and result.get("__error__"):
                logger.error(
                    "搜索接口失败: %s, 响应片段: %s",
                    result["__error__"],
                    result.get("__text__", "")[:500],
                )
                return []

            papers = []
            if isinstance(result, dict):
                records = result.get("records", [])[:最大数量]
            elif isinstance(result, list):
                records = result[:最大数量]
            else:
                logger.error(f"未知返回类型: {type(result)}")
                return []

            for item in records:
                if not isinstance(item, dict):
                    continue

                authors = item.get("authors", [])
                if not isinstance(authors, list):
                    authors = []

                paper = {
                    "标题": item.get("articleTitle", ""),
                    "作者": [
                        a.get("preferredName", a.get("name", ""))
                        if isinstance(a, dict)
                        else str(a)
                        for a in authors
                    ],
                    "摘要": item.get("abstract", ""),
                    "发表日期": item.get("publicationDate", ""),
                    "发表年份": item.get("publicationYear", ""),
                    "期刊/会议": item.get("journalName", "")
                    or item.get("conferenceName", ""),
                    "DOI": item.get("doi", ""),
                    "文章编号": item.get("articleNumber", ""),
                    "IEEE_URL": f"https://ieeexplore.ieee.org/document/{item.get('articleNumber', '')}",
                }
                papers.append(paper)

            logger.info(f"找到 {len(papers)} 篇论文")
            return papers

        except Exception as e:
            logger.error(f"搜索失败: {e}")
            import traceback

            traceback.print_exc()
            return []

    def _获取PDF内容(self, 文章编号: str) -> Optional[bytes]:
        """优先通过详情页真实进入 stamp 页面并提取 PDF；最后才回退到直连。"""
        try:
            pdf_body = fetch_pdf_bytes_via_document_page(self.context, 文章编号, page=self.page)
            if pdf_body:
                return pdf_body
            logger.warning("页面方式未拿到有效PDF，回退到直连 getPDF.jsp")
        except Exception as e:
            logger.warning(f"页面方式下载PDF失败，回退到直连: {e}")

        pdf_url = f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={文章编号}&ref="
        try:
            response = self.context.request.get(pdf_url, timeout=60000)
            body = response.body()
            content_type = response.headers.get("content-type", "")
            if body.startswith(b"%PDF"):
                return body

            snippet = body[:300].decode("utf-8", errors="ignore").replace("\n", " ")
            logger.warning(
                "直连PDF响应无效: status=%s, content-type=%s, url=%s, body片段=%s",
                response.status,
                content_type or "(空)",
                response.url,
                snippet,
            )
        except Exception as e:
            logger.error(f"直连PDF下载失败: {e}")
        return None

    def 下载PDF(self, 文章编号: str, 保存文件名: str = None) -> Optional[str]:
        """下载论文PDF"""
        if 保存文件名 is None:
            保存文件名 = f"{文章编号}.pdf"

        保存路径 = self.输出目录 / 保存文件名
        pdf_content = self._获取PDF内容(文章编号)
        if not pdf_content:
            return None

        with open(保存路径, "wb") as f:
            f.write(pdf_content)

        logger.info(f"PDF已保存: {保存路径}")
        return str(保存路径)

    def 下载PDF到文件(self, 文章编号: str, 标题: str) -> Optional[str]:
        """下载论文PDF到文件"""
        try:
            # 清理文件名
            rstr = r"[\=\(\)\,\/\\\:\*\?\？\"\<\>\|\'']"
            safe_title = re.sub(rstr, "", 标题)[:100]
            保存文件名 = f"{safe_title}_{文章编号}.pdf"

            if self.保存登录态:
                self.保存登录态到文件()

            保存路径 = self.输出目录 / 保存文件名
            pdf_content = self._获取PDF内容(文章编号)
            if not pdf_content:
                return None

            with open(保存路径, "wb") as f:
                f.write(pdf_content)
            logger.info(f"PDF已保存: {保存路径}")
            return str(保存路径)

        except Exception as e:
            logger.error(f"PDF下载失败: {e}")
            return None

    def 关闭浏览器(self):
        """关闭浏览器"""
        if self.保存登录态:
            self.保存登录态到文件()

        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()

    def 保存元数据(self, 论文列表: list, 文件名: str = "papers.json"):
        """保存论文元数据到JSON文件"""
        保存路径 = self.输出目录 / 文件名

        with open(保存路径, "w", encoding="utf-8") as f:
            json.dump(论文列表, f, ensure_ascii=False, indent=2)

        logger.info(f"元数据已保存: {保存路径}")
        return str(保存路径)


def 交互式搜索():
    """交互式搜索模式"""
    print("\n" + "=" * 50)
    print("IEEE Xplore 论文抓取工具 (Playwright版)")
    print("=" * 50)
    print("\n提示：首次运行请在浏览器中登录账号")
    print("登录后登录态会自动保存，下次运行无需再登录")

    爬虫 = IEEE爬虫()

    try:
        while True:
            print("\n" + "-" * 40)
            关键词 = input("请输入搜索关键词 (输入 'q' 退出): ").strip()
            if 关键词.lower() == "q":
                break

            数量 = input("请输入要获取的论文数量 [默认10]: ").strip()
            数量 = int(数量) if 数量.isdigit() else 10

            print(f"\n搜索关键词: {关键词}")
            论文列表 = 爬虫.搜索论文(关键词, 数量)

            if 论文列表:
                print(f"\n找到 {len(论文列表)} 篇论文:\n")
                for i, paper in enumerate(论文列表, 1):
                    print(f"{i}. {paper['标题'][:70]}...")
                    print(f"   作者: {', '.join(paper['作者'][:2])}")
                    print(f"   日期: {paper['发表日期'] or paper['发表年份']}")
                    print()

                print("\n选择操作:")
                print("  s - 保存元数据到JSON")
                print("  d - 下载PDF (逐个)")
                print("  a - 下载所有PDF")
                print("  n - 继续搜索")

                选择 = input("请选择 [s/d/a/n]: ").strip().lower()

                if 选择 == "s":
                    爬虫.保存元数据(论文列表)
                    print("元数据已保存!")
                elif 选择 == "d":
                    for i, paper in enumerate(论文列表, 1):
                        文章编号 = paper.get("文章编号")
                        标题 = paper.get("标题", "")
                        if not 文章编号:
                            continue
                        print(f"[{i}/{len(论文列表)}] 下载: {标题[:40]}...")
                        结果 = 爬虫.下载PDF到文件(文章编号, 标题)
                        if 结果:
                            print(f"  -> 成功: {结果}")
                        else:
                            print(f"  -> 失败 (可能需要权限)")
                    print("\n下载完成!")
                elif 选择 == "a":
                    print("\n开始批量下载...")
                    for i, paper in enumerate(论文列表, 1):
                        文章编号 = paper.get("文章编号")
                        标题 = paper.get("标题", "")
                        if not 文章编号:
                            continue
                        print(f"[{i}/{len(论文列表)}] 下载: {标题[:40]}...")
                        结果 = 爬虫.下载PDF到文件(文章编号, 标题)
                        if 结果:
                            print(f"  -> 成功")
                        else:
                            print(f"  -> 失败")
                        time.sleep(1)
                    print("\n批量下载完成!")
            else:
                print("未找到相关论文")
    finally:
        爬虫.关闭浏览器()


if __name__ == "__main__":
    交互式搜索()

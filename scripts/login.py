#!/usr/bin/env python3
"""
IEEE Xplore 自动机构登录工具

已验证流程：
IEEE -> Institutional Sign In -> Access Through Your Institution / 记住机构入口
-> 机构 SSO iframe 真人式输入 -> 返回 IEEE 并保存登录态
"""

from playwright.sync_api import sync_playwright

from _bootstrap import bootstrap_project_root

bootstrap_project_root()

from ieee_harvest.auth import (
    DEFAULT_STATE_FILE,
    auto_login_ieee_institution,
    create_ieee_context,
    has_ieee_institutional_access,
    load_ieee_credentials,
)


def main() -> None:
    credentials = load_ieee_credentials()
    print("=" * 60)
    print("IEEE Xplore 自动机构登录")
    print("=" * 60)
    print(f"目标机构: {credentials['IEEE_INST_NAME']}")
    print(f"登录态输出: {DEFAULT_STATE_FILE}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = create_ieee_context(browser, DEFAULT_STATE_FILE)
        page = context.new_page()
        page.set_default_timeout(60000)

        if has_ieee_institutional_access(page, context):
            print("检测到已有 IEEE 机构访问态，直接保存并退出。")
            context.storage_state(path=str(DEFAULT_STATE_FILE))
        else:
            print("开始自动登录机构 SSO ...")
            auto_login_ieee_institution(page, context, credentials, DEFAULT_STATE_FILE)
            print("自动登录成功。")

        print(f"登录态已保存到: {DEFAULT_STATE_FILE}")
        print("你现在可以继续执行下载脚本了。")

        browser.close()


if __name__ == "__main__":
    main()

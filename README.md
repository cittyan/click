- 上游源码: https://github.com/pallets/click
- 官方文档: https://click.palletsprojects.com/en/stable/

---

<div align="center"><img src="https://raw.githubusercontent.com/pallets/click/refs/heads/stable/docs/_static/click-name.svg" alt="" height="150"></div>

# Click

Click 是一个 Python 包，用于以组合方式创建美观的命令行界面，且所需代码量尽可能少。它被称为“命令行界面创建工具包”。它具有高度可配置性，但同时也提供了开箱即用的合理默认设置。

Click 的目标是让编写命令行工具的过程既快速又有趣，同时避免因无法实现预期的 CLI API 而产生的挫败感。

Click 的三大特点:

-   命令的任意嵌套
-   自动生成帮助页面
-   支持在运行时对子命令进行延迟加载


## 一个简单的例子

```python
import click

@click.command()
@click.option("--count", default=1, help="问候的次数")
@click.option("--name", prompt="你的名字", help="要问候的人")
def hello(count, name):
    """一个简单的程序，会向 NAME 打招呼总共 COUNT 次"""
    for _ in range(count):
        click.echo(f"Hello, {name}!")

if __name__ == '__main__':
    hello()
```

```
$ python hello.py --count=3
你的名字: Click
Hello, Click!
Hello, Click!
Hello, Click!
```


## 捐赠

Pallets 组织开发并支持 Click 及其他热门软件包。为了壮大贡献者和用户社区，并让维护者能够投入更多时间到项目中，[请立即捐款][]。

[请立即捐款]: https://palletsprojects.com/donate

## 贡献

请参阅我们的 [详细贡献文档][贡献]，了解多种贡献方式，包括报告问题、请求功能、提问或回答问题，以及提交PR。

[贡献]: https://palletsprojects.com/contributing/

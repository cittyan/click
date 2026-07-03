from __future__ import annotations

import collections.abc as cabc
import os
import re
import sys
import typing as t
from functools import update_wrapper
from gettext import gettext as _
from types import ModuleType
from types import TracebackType

from ._compat import _default_text_stderr
from ._compat import _default_text_stdout
from ._compat import _find_binary_writer
from ._compat import auto_wrap_for_ansi
from ._compat import binary_streams
from ._compat import open_stream
from ._compat import should_strip_ansi
from ._compat import strip_ansi
from ._compat import text_streams
from ._compat import WIN
from .globals import resolve_color_default

if t.TYPE_CHECKING:
    import typing_extensions as te

    P = te.ParamSpec("P")

R = t.TypeVar("R")


def _posixify(name: str) -> str:
    return "-".join(name.split()).lower()


def safecall(func: t.Callable[P, R]) -> t.Callable[P, R | None]:
    """包装一个函数，使其吞没异常"""

    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
        try:
            return func(*args, **kwargs)
        except Exception:
            pass
        return None

    return update_wrapper(wrapper, func)


def make_str(value: t.Any) -> str:
    """将一个值转换为有效的字符串"""
    if isinstance(value, bytes):
        try:
            return value.decode(sys.getfilesystemencoding())
        except UnicodeError:
            return value.decode("utf-8", "replace")
    return str(value)


def make_default_short_help(help: str, max_length: int = 45) -> str:
    """返回帮助字符串的精简版本

    :meta private:
    """
    # Consider only the first paragraph.
    paragraph_end = help.find("\n\n")

    if paragraph_end != -1:
        help = help[:paragraph_end]

    # Collapse newlines, tabs, and spaces.
    words = help.split()

    if not words:
        return ""

    # The first paragraph started with a "no rewrap" marker, ignore it.
    if words[0] == "\b":
        words = words[1:]

    total_length = 0
    last_index = len(words) - 1

    for i, word in enumerate(words):
        total_length += len(word) + (i > 0)

        if total_length > max_length:  # too long, truncate
            break

        if word[-1] == ".":  # sentence end, truncate without "..."
            return " ".join(words[: i + 1])

        if total_length == max_length and i != last_index:
            break  # not at sentence end, truncate with "..."
    else:
        return " ".join(words)  # no truncation needed

    # Account for the length of the suffix.
    total_length += len("...")

    # remove words until the length is short enough
    while i > 0:
        total_length -= len(words[i]) + (i > 0)

        if total_length <= max_length:
            break

        i -= 1

    return " ".join(words[:i]) + "..."


class LazyFile:
    """
    懒加载文件的工作方式与普通文件类似，但它不会完全打开文件，而是会提前执行一些基本检查，以确认文件名参数是否合理。

    这对于安全地打开文件进行写入操作非常有用。懒加载文件的工作方式与普通文件类似，但它不会完全打开文件，而是会提前执行一些基本检查，以确认文件名参数是否合理。这对于安全地打开文件进行写入操作非常有用。
    """

    name: str
    mode: str
    encoding: str | None
    errors: str | None
    atomic: bool
    _f: t.IO[t.Any] | None
    should_close: bool

    def __init__(
        self,
        filename: str | os.PathLike[str],
        mode: str = "r",
        encoding: str | None = None,
        errors: str | None = "strict",
        atomic: bool = False,
    ) -> None:
        self.name = os.fspath(filename)
        self.mode = mode
        self.encoding = encoding
        self.errors = errors
        self.atomic = atomic

        if self.name == "-":
            self._f, self.should_close = open_stream(filename, mode, encoding, errors)
        else:
            if "r" in mode:
                # Open and close the file in case we're opening it for
                # reading so that we can catch at least some errors in
                # some cases early.
                open(filename, mode).close()
            self._f = None
            self.should_close = True

    def __getattr__(self, name: str) -> t.Any:
        return getattr(self.open(), name)

    def __repr__(self) -> str:
        if self._f is not None:
            return repr(self._f)
        return f"<未打开的文件 '{format_filename(self.name)}' {self.mode}>"

    def open(self) -> t.IO[t.Any]:
        """
        如果文件尚未打开，则打开文件。

        此调用可能会因 :exc:`FileError` 而失败。如果不处理此错误，Click 会显示一个错误。
        """
        if self._f is not None:
            return self._f
        try:
            rv, self.should_close = open_stream(
                self.name, self.mode, self.encoding, self.errors, atomic=self.atomic
            )
        except OSError as e:
            from .exceptions import FileError

            raise FileError(self.name, hint=e.strerror) from e
        self._f = rv
        return rv

    def close(self) -> None:
        """关闭底层文件，无论如何"""
        if self._f is not None:
            self._f.close()

    def close_intelligently(self) -> None:
        """
        此函数仅在文件由惰性文件包装器打开时才会关闭该文件。

        例如，它永远不会关闭标准输入（stdin）。
        """
        if self.should_close:
            self.close()

    def __enter__(self) -> LazyFile:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close_intelligently()

    def __iter__(self) -> cabc.Iterator[t.AnyStr]:
        self.open()
        return iter(self._f)  # type: ignore


class KeepOpenFile:
    _file: t.IO[t.Any]

    def __init__(self, file: t.IO[t.Any]) -> None:
        self._file = file

    def __getattr__(self, name: str) -> t.Any:
        return getattr(self._file, name)

    def __enter__(self) -> KeepOpenFile:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        pass

    def __repr__(self) -> str:
        return repr(self._file)

    def __iter__(self) -> cabc.Iterator[t.AnyStr]:
        return iter(self._file)


def echo(
    message: object = None,
    file: t.IO[t.Any] | None = None,
    nl: bool = True,
    err: bool = False,
    color: bool | None = None,
) -> None:
    """将消息和换行符打印到标准输出或文件中。应使用此方法代替 :func:`print`，
    因为它能更好地支持不同的数据、文件和环境。

    与 :func:`print` 相比，它具有以下功能:

    -   确保在 Linux 上输出编码不会配置错误
    -   支持 Windows 控制台中的 Unicode
    -   支持写入二进制输出，并且支持将字节写入文本输出
    -   在 Windows 上支持颜色和样式
    -   如果输出看起来不像交互式终端，则移除 ANSI 颜色和样式代码
    -   始终刷新输出

    :param message: 要输出的字符串或字节。其他对象会被转换为字符串
    :param file: 要写入的文件。默认为 ``stdout``
    :param err: 写入 ``stderr`` 而不是 ``stdout``
    :param nl: 在消息后打印一个换行符。默认启用
    :param color: 强制显示或隐藏颜色和其他样式。默认情况下，如果输出看起来不像交互式终端，Click 会移除颜色

    .. versionchanged:: 6.0
        支持在 Windows 控制台上输出 Unicode。点击操作不会修改 ``sys.stdout``，因此 ``sys.stdout.write()`` 和 ``print()`` 仍然不支持 Unicode。

    .. versionchanged:: 4.0
        添加了``color``参数。

    .. versionadded:: 3.0
        添加了``err``参数。

    .. versionchanged:: 2.0
        如果安装了colorama，则在Windows上支持颜色。
    """
    if file is None:
        if err:
            file = _default_text_stderr()
        else:
            file = _default_text_stdout()

        # There are no standard streams attached to write to. For example,
        # pythonw on Windows.
        if file is None:
            return

    match message:
        case str() | bytes() | bytearray():
            out = message
        case None:
            out = ""
        case _:
            out = str(message)

    if nl:
        if isinstance(out, str):
            out += "\n"
        else:
            out += b"\n"

    if not out:
        file.flush()
        return

    # If there is a message and the value looks like bytes, we manually
    # need to find the binary stream and write the message in there.
    # This is done separately so that most stream types will work as you
    # would expect. Eg: you can write to StringIO for other cases.
    if isinstance(out, (bytes, bytearray)):
        binary_file = _find_binary_writer(file)
        if binary_file is not None:
            file.flush()
            binary_file.write(out)
            binary_file.flush()
            return

    # ANSI style code support. For no message or bytes, nothing happens.
    # When outputting to a file instead of a terminal, strip codes.
    else:
        color = resolve_color_default(color)

        if should_strip_ansi(file, color):
            out = strip_ansi(out)
        elif WIN:
            if auto_wrap_for_ansi is not None:
                file = auto_wrap_for_ansi(file, color)  # type: ignore
            elif not color:
                out = strip_ansi(out)

    file.write(out)  # type: ignore
    file.flush()


def get_binary_stream(name: t.Literal["stdin", "stdout", "stderr"]) -> t.BinaryIO:
    """
    返回一个用于字节处理的系统流

    :param name: 要打开的流名称，有效的名称是 ``'stdin'``, ``'stdout'`` 和 ``'stderr'``
    """
    opener = binary_streams.get(name)
    if opener is None:
        raise TypeError(_("未知的标准流 '{name}'").format(name=name))
    return opener()


def get_text_stream(
    name: t.Literal["stdin", "stdout", "stderr"],
    encoding: str | None = None,
    errors: str | None = "strict",
) -> t.TextIO:
    """
    返回用于文本处理的系统流。

    这通常返回一个包装流，该流基于从 :func:`get_binary_stream` 返回的二进制流，但对于已经正确配置的流，也可以采取快捷方式。

    :param name: 要打开的流名称，有效的名称是 ``'stdin'``, ``'stdout'`` 和 ``'stderr'``
    :param encoding: 覆盖检测到的默认编码
    :param errors: 覆盖默认错误模式
    """
    opener = text_streams.get(name)
    if opener is None:
        raise TypeError(_("未知的标准流 '{name}'").format(name=name))
    return opener(encoding, errors)


def open_file(
    filename: str | os.PathLike[str],
    mode: str = "r",
    encoding: str | None = None,
    errors: str | None = "strict",
    lazy: bool = False,
    atomic: bool = False,
) -> t.IO[t.Any]:
    """
    打开文件，具有额外行为以处理 ``'-'`` 表示标准流、写入时延迟打开以及原子写入。类似于 :class:`~click.File` 参数类型的行为。

    如果将 `'-'` 传递给 `stdout` 或 `stdin` 以打开它们，该流会被包装，从而在使用上下文管理器时不会关闭它。

    这使得可以在不意外关闭标准流的情况下使用该函数:

    .. code-block:: python

        with open_file(filename) as f:
            ...

    :param filename: 要打开的文件的名称或路径，或者使用 `'-'` 表示 `stdin`/`stdout`
    :param mode: 打开文件时的模式
    :param encoding: 在文本模式下打开文件时用于解码或编码文件的编码方式
    :param errors: 错误处理模式
    :param lazy: 等待文件被访问时再打开文件。对于读取模式，文件会暂时打开以尽早引发访问错误，然后关闭直到再次读取
    :param atomic: 写入临时文件，并在关闭时替换给定的文件

    .. versionadded:: 3.0
    """
    if lazy:
        return t.cast(
            "t.IO[t.Any]", LazyFile(filename, mode, encoding, errors, atomic=atomic)
        )

    f, should_close = open_stream(filename, mode, encoding, errors, atomic=atomic)

    if not should_close:
        f = t.cast("t.IO[t.Any]", KeepOpenFile(f))

    return f


def format_filename(
    filename: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    shorten: bool = False,
) -> str:
    """
    将文件名格式化为字符串以供显示。通过将名称中的任何无效字节或代理转义字符替换为替换字符 ``�``，确保文件名可以显示。

    当使用 ``errors="strict"`` 将无效字节或代理转义字符写入流时，会引发错误。这种情况通常发生在 ``stdout`` 上，当区域设置为类似 ``en_GB.UTF-8`` 的值时。

    不过，由于 PEP 538 和 PEP 540 的规定，许多场景下写入代理转义字符是安全的，包括:

    -   写入到 `stderr`，使用 `errors="backslashreplace"`
    -   系统设置为 `LANG=C.UTF-8`、`C` 或 `POSIX`。Python 以 `errors="surrogateescape"` 打开标准输出和标准错误
    -   未设置任何 `LANG/LC_*`。Python 假设 `LANG=C.UTF-8`
    -   Python 以 UTF-8 模式启动，使用 `PYTHONUTF8=1` 或 `-X utf8`。Python 以 `errors="surrogateescape"` 打开标准输出和标准错误

    :param filename: 格式化文件名以供 UI 显示。这也会将文件名转换为 Unicode，而不会导致失败
    :param shorten: 此操作可选择性地缩短文件名，去除其前导路径
    """
    if shorten:
        filename = os.path.basename(filename)
    else:
        filename = os.fspath(filename)

    if isinstance(filename, bytes):
        filename = filename.decode(sys.getfilesystemencoding(), "replace")
    else:
        filename = filename.encode("utf-8", "surrogateescape").decode(
            "utf-8", "replace"
        )

    return filename


def get_app_dir(app_name: str, roaming: bool = True, force_posix: bool = False) -> str:
    r"""
    返回应用程序的配置文件夹。默认行为是返回最适合操作系统的选项。

    举个例子，对于一个名为``"Foo Bar"``的应用程序，可能会返回以下类似的文件夹:

    Mac OS X:
      ``~/Library/Application Support/Foo Bar``
    Mac OS X (POSIX):
      ``~/.foo-bar``
    Unix:
      ``~/.config/foo-bar``
    Unix (POSIX):
      ``~/.foo-bar``
    Windows (roaming):
      ``C:\Users\<user>\AppData\Roaming\Foo Bar``
    Windows (not roaming):
      ``C:\Users\<user>\AppData\Local\Foo Bar``

    .. versionadded:: 2.0

    :param app_name: 应用程序名称。应正确使用大小写，并且可以包含空格。
    :param roaming: 控制文件夹在 Windows 上是否应漫游。否则无影响。
    :param force_posix: 如果此设置设为 `True`，那么在任何 POSIX 系统上，该文件夹将存储在主目录中，并以点开头，而不是存储在 XDG 配置目录或 Darwin 的应用程序支持文件夹中。
    """
    if WIN:
        key = "APPDATA" if roaming else "LOCALAPPDATA"
        folder = os.environ.get(key)
        if folder is None:
            folder = os.path.expanduser("~")
        return os.path.join(folder, app_name)
    if force_posix:
        return os.path.join(os.path.expanduser(f"~/.{_posixify(app_name)}"))
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~/Library/Application Support"), app_name
        )
    return os.path.join(
        os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
        _posixify(app_name),
    )


class PacifyFlushWrapper:
    """
    此包装器用于捕获并抑制在 Python 解释器关闭/最终垃圾回收期间，对已断开连接的管道调用 `.flush()` 所导致的 `BrokenPipeError`。

    值得注意的是，`.flush()` 总是会在 `sys.stdout` 和 `sys.stderr` 上被调用。

    为了对其他清理代码的影响降到最低，并且处理底层文件并非断开连接管道的情况，所有调用和属性均通过代理实现。
    """

    wrapped: t.IO[t.Any]

    def __init__(self, wrapped: t.IO[t.Any]) -> None:
        self.wrapped = wrapped

    def flush(self) -> None:
        try:
            self.wrapped.flush()
        except OSError as e:
            import errno

            if e.errno != errno.EPIPE:
                raise

    def __getattr__(self, attr: str) -> t.Any:
        return getattr(self.wrapped, attr)


def _detect_program_name(
    path: str | None = None, _main: ModuleType | None = None
) -> str:
    """
    确定用于运行程序的命令，以便在帮助文本中使用。如果执行了文件或入口点，则返回文件名。如果使用了 ``python -m`` 来执行模块或包，则返回 ``python -m name``。

    此功能并不追求过于精确，其目的是为帮助文本提供一个简洁的名称。文件仅显示其名称而不包含路径。对于模块，仅显示 ``python``，且不显示 ``sys.executable`` 的完整路径。

    :param path: 正在执行的Python文件。Python将其放入``sys.argv[0]``中，该参数默认被使用
    :param _main: ``__main__``模块。这只应在内部测试期间传递

    .. versionadded:: 8.0
        基于 Werkzeug 重载器中的命令参数检测。

    :meta private:
    """
    if _main is None:
        _main = sys.modules["__main__"]

    if not path:
        path = sys.argv[0]

    # The value of __package__ indicates how Python was called. It may
    # not exist if a setuptools script is installed as an egg. It may be
    # set incorrectly for entry points created with pip on Windows.
    # It is set to "" inside a Shiv or PEX zipapp.
    if getattr(_main, "__package__", None) in {None, ""} or (
        os.name == "nt"
        and _main.__package__ == ""
        and not os.path.exists(path)
        and os.path.exists(f"{path}.exe")
    ):
        # Executed a file, like "python app.py".
        return os.path.basename(path)

    # Executed a module, like "python -m example".
    # Rewritten by Python from "-m script" to "/path/to/script.py".
    # Need to look at main module to determine how it was executed.
    py_module = t.cast(str, _main.__package__)
    name = os.path.splitext(os.path.basename(path))[0]

    # A submodule like "example.cli".
    if name != "__main__":
        py_module = f"{py_module}.{name}"

    return f"python -m {py_module.lstrip('.')}"


def _expand_args(
    args: cabc.Iterable[str],
    *,
    user: bool = True,
    env: bool = True,
    glob_recursive: bool = True,
) -> list[str]:
    """
    使用 Python 函数模拟 Unix shell 的扩展功能。

    请参阅 :func:`glob.glob`, :func:`os.path.expanduser` 和 :func:`os.path.expandvars`。

    该功能主要用于 Windows 系统，因为 Windows shell 不会进行任何扩展。它可能无法完全匹配 Unix shell 的行为。

    :param args: 命令行参数扩展列表
    :param user: 扩展用户主目录
    :param env: 扩展环境变量
    :param glob_recursive: ``**`` 递归匹配目录

    .. versionchanged:: 8.1
        无效的 glob 模式被视为空扩展，而不是引发错误。

    .. versionadded:: 8.0

    :meta private:
    """
    from glob import glob

    out = []

    for arg in args:
        if user:
            arg = os.path.expanduser(arg)

        if env:
            arg = os.path.expandvars(arg)

        try:
            matches = glob(arg, recursive=glob_recursive)
        except re.error:
            matches = []

        if not matches:
            out.append(arg)
        else:
            out.extend(matches)

    return out

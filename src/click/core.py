from __future__ import annotations

import collections.abc as cabc
import enum
import errno
import inspect
import os
import sys
import typing as t
from abc import ABC
from abc import abstractmethod
from collections import abc
from collections import Counter
from contextlib import AbstractContextManager
from contextlib import contextmanager
from contextlib import ExitStack
from functools import update_wrapper
from gettext import gettext as _
from gettext import ngettext
from itertools import repeat
from types import TracebackType

from . import types
from ._utils import FLAG_NEEDS_VALUE
from ._utils import UNSET
from .exceptions import Abort
from .exceptions import BadParameter
from .exceptions import ClickException
from .exceptions import Exit
from .exceptions import MissingParameter
from .exceptions import NoArgsIsHelpError
from .exceptions import NoSuchCommand
from .exceptions import UsageError
from .formatting import HelpFormatter
from .formatting import join_options
from .globals import pop_context
from .globals import push_context
from .parser import _OptionParser
from .parser import _split_opt
from .termui import confirm
from .termui import prompt
from .termui import style
from .utils import _detect_program_name
from .utils import _expand_args
from .utils import echo
from .utils import make_default_short_help
from .utils import make_str
from .utils import PacifyFlushWrapper

if t.TYPE_CHECKING:
    from .shell_completion import CompletionItem

F = t.TypeVar("F", bound="t.Callable[..., t.Any]")
V = t.TypeVar("V")


def _complete_visible_commands(
    ctx: Context, incomplete: str
) -> cabc.Iterator[tuple[str, Command]]:
    """列出以不完整值开头且未被隐藏的该组的所有子命令

    :param ctx: 组的调用上下文
    :param incomplete: 正在补全的值。可能为空
    """
    multi = t.cast(Group, ctx.command)

    for name in multi.list_commands(ctx):
        if name.startswith(incomplete):
            command = multi.get_command(ctx, name)

            if command is not None and not command.hidden:
                yield name, command


def _check_nested_chain(
    base_command: Group, cmd_name: str, cmd: Command, register: bool = False
) -> None:
    if not base_command.chain or not isinstance(cmd, Group):
        return

    if register:
        message = (
            f"无法将组 {cmd_name!r} 添加到处于链式模式的另一个组 {base_command.name!r} 中"
        )
    else:
        message = (
            f"发现组 {cmd_name!r} 作为另一个组 {base_command.name!r} 的子命令，而该组处于链式模式。此操作不受支持！"
        )

    raise RuntimeError(message)


def _format_deprecated_label(deprecated: bool | str) -> str:
    """返回帮助文本中显示的括号化弃用标签"""
    # label = _("deprecated").upper()
    label = "已弃用"
    if isinstance(deprecated, str):
        return f"({label}: {deprecated})"
    return f"({label})"


def _format_deprecated_suffix(deprecated: bool | str) -> str:
    """
    返回一个 ``弃用警告`` 消息的尾部原因，前面加上一个空格。

    如果没有给出原因，则返回空字符串。
    """
    if isinstance(deprecated, str):
        return f" {deprecated}"
    return ""


def batch(iterable: cabc.Iterable[V], batch_size: int) -> list[tuple[V, ...]]:
    return list(zip(*repeat(iter(iterable), batch_size), strict=False))


@contextmanager
def augment_usage_errors(
    ctx: Context, param: Parameter | None = None
) -> cabc.Iterator[None]:
    """上下文管理器，用于将额外信息附加到异常上"""
    try:
        yield
    except BadParameter as e:
        if e.ctx is None:
            e.ctx = ctx
        if param is not None and e.param is None:
            e.param = param
        raise
    except UsageError as e:
        if e.ctx is None:
            e.ctx = ctx
        raise


def iter_params_for_processing(
    invocation_order: cabc.Sequence[Parameter],
    declaration_order: cabc.Sequence[Parameter],
) -> list[Parameter]:
    """
    按应处理的顺序返回所有已声明的参数。

    已声明的参数会根据调用顺序以及每个参数的“急切性”（eagerness）重新排列。

    调用顺序优先于声明顺序。也就是说，用户向 CLI 提供参数的顺序将被保留。

    此行为及其对回调函数评估的影响详见：
    https://click.palletsprojects.com/en/stable/advanced/#callback-evaluation-order
    """

    def sort_key(item: Parameter) -> tuple[bool, float]:
        try:
            idx: float = invocation_order.index(item)
        except ValueError:
            idx = float("inf")

        return not item.is_eager, idx

    return sorted(declaration_order, key=sort_key)


class ParameterSource(enum.IntEnum):
    """
    这是一个 `:class:`~enum.IntEnum`，用于指示参数值的来源。

    使用 :meth:`click.Context.get_parameter_source` 方法可以通过参数名称获取其来源。

    成员按从最显式到最不显式的来源顺序排列。
    这允许进行比较以检查值是否被显式提供：

    .. code-block:: python

        source = ctx.get_parameter_source("port")
        if source < click.ParameterSource.DEFAULT_MAP:
            ...  # value was explicitly set

    .. versionchanged:: 8.3.3
        使用 :class:`~enum.IntEnum` 并将成员按从最明确到最不明确的顺序重新排列。支持比较运算符。

    .. versionchanged:: 8.0
        使用 :class:`~enum.Enum` 并删除 ``validate`` 方法。

    .. versionchanged:: 8.0
        添加了 ``PROMPT`` 值。
    """

    PROMPT = enum.auto()
    """使用提示来确认默认值或提供值"""
    COMMANDLINE = enum.auto()
    """该值由命令行参数提供"""
    ENVIRONMENT = enum.auto()
    """该值通过环境变量提供"""
    DEFAULT_MAP = enum.auto()
    """使用了 :attr:`Context.default_map` 提供的默认值"""
    DEFAULT = enum.auto()
    """使用了参数指定的默认值"""


class Context:
    """
    上下文是一个特殊的内部对象，它保存了与脚本在每一层级执行相关的状态。

    通常情况下，命令无法直接访问它，除非命令主动选择获取访问权限。

    上下文非常有用，因为它可以传递内部对象，并可以控制一些特殊的执行特性，例如从环境变量中读取数据。

    上下文可以用作上下文管理器，在这种情况下，它会在销毁时调用 `:meth:`close`` 方法。

    :param command: 该上下文的命令类
    :param parent: 父上下文
    :param info_name: 本次调用的信息名称。通常这是脚本或命令最具描述性的名称。对于顶层脚本，它通常是脚本的名称；对于其下的命令，则是脚本的名称
    :param obj: 一个任意的用户数据对象
    :param auto_envvar_prefix: 用于自动环境变量的前缀。如果此值为 `None`，则禁用从环境变量中读取。这不会影响手动设置的环境变量，它们始终会被读取
    :param default_map: 一个包含参数默认值的字典（类似对象）
    :param terminal_width: 终端的宽度。默认值是从父上下文中继承。如果没有上下文定义终端宽度，则将应用自动检测
    :param max_content_width: Click 渲染内容的最大宽度（目前仅影响帮助页面）。如果未覆盖，则默认为 80 个字符。换句话说：即使终端宽度大于该值，Click 默认也不会将内容格式化为超过 80 个字符。此外，格式化器可能会在右侧添加一些安全边距
    :param resilient_parsing: 如果启用此标志，则 Click 将解析而不进行任何交互或回调调用。默认值也将被忽略。这对于实现诸如补全支持等功能很有用
    :param allow_extra_args: 如果设置为 `True`，则末尾的额外参数不会引发错误，并且会保留在上下文中。默认值是从命令继承
    :param allow_interspersed_args: 如果设置为 `False`，则选项和参数不能混用。默认值是从命令继承
    :param ignore_unknown_options: 指示 Click 忽略它不识别的选项，并将其保留以供后续处理
    :param help_option_names: 可选地，一个字符串列表，用于定义默认帮助参数的名称。默认值为 ``['--help']``
    :param token_normalize_func: 一个可选函数，用于规范化标记（选项、选择项等）。例如，可以用于实现大小写不敏感的行为
    :param color: 控制终端是否支持 ANSI 颜色。默认值为自动检测。仅在 Click 打印的文本中使用了 ANSI 代码时才需要设置此值（默认情况下不会使用）。例如，这会影响帮助输出
    :param show_default: 显示命令的默认值。如果未设置此值，则默认为父上下文的值。``Command.show_default`` 会覆盖特定命令的此默认值

    .. versionchanged:: 8.2
        ``protected_args`` 属性已被弃用，并将在 Click 9.0 中移除。``args`` 将包含剩余的未解析标记。

    .. versionchanged:: 8.1
        ``show_default`` 参数现在由 ``Command.show_default`` 覆盖，而不是相反。

    .. versionchanged:: 8.0
        ``show_default`` 参数的默认值来自父上下文。

    .. versionchanged:: 7.1
        添加了 ``show_default`` 参数。

    .. versionchanged:: 4.0
        添加了 ``color``、``ignore_unknown_options`` 和 ``max_content_width`` 参数。

    .. versionchanged:: 3.0
        添加了 ``allow_extra_args`` 和 ``allow_interspersed_args`` 参数。

    .. versionchanged:: 2.0
        添加了 ``resilient_parsing``、``help_option_names`` 和 ``token_normalize_func`` 参数。
    """

    #: The formatter class to create with :meth:`make_formatter`.
    #:
    #: .. versionadded:: 8.0
    formatter_class: type[HelpFormatter] = HelpFormatter

    def __init__(
        self,
        command: Command,
        parent: Context | None = None,
        info_name: str | None = None,
        obj: t.Any | None = None,
        auto_envvar_prefix: str | None = None,
        default_map: cabc.MutableMapping[str, t.Any] | None = None,
        terminal_width: int | None = None,
        max_content_width: int | None = None,
        resilient_parsing: bool = False,
        allow_extra_args: bool | None = None,
        allow_interspersed_args: bool | None = None,
        ignore_unknown_options: bool | None = None,
        help_option_names: list[str] | None = None,
        token_normalize_func: t.Callable[[str], str] | None = None,
        color: bool | None = None,
        show_default: bool | None = None,
    ) -> None:
        #: the parent context or `None` if none exists.
        self.parent = parent
        #: the :class:`Command` for this context.
        self.command = command
        #: the descriptive information name
        self.info_name = info_name
        #: Map of parameter names to their parsed values. Parameters
        #: with ``expose_value=False`` are not stored.
        self.params: dict[str, t.Any] = {}
        #: the leftover arguments.
        self.args: list[str] = []
        #: protected arguments.  These are arguments that are prepended
        #: to `args` when certain parsing scenarios are encountered but
        #: must be never propagated to another arguments.  This is used
        #: to implement nested parsing.
        self._protected_args: list[str] = []
        #: the collected prefixes of the command's options.
        self._opt_prefixes: set[str] = set(parent._opt_prefixes) if parent else set()

        if obj is None and parent is not None:
            obj = parent.obj

        #: the user object stored.
        self.obj: t.Any = obj
        self._meta: dict[str, t.Any] = getattr(parent, "meta", {})

        #: A dictionary (-like object) with defaults for parameters.
        if (
            default_map is None
            and info_name is not None
            and parent is not None
            and parent.default_map is not None
        ):
            default_map = parent.default_map.get(info_name)

        self.default_map: cabc.MutableMapping[str, t.Any] | None = default_map

        #: This flag indicates if a subcommand is going to be executed. A
        #: group callback can use this information to figure out if it's
        #: being executed directly or because the execution flow passes
        #: onwards to a subcommand. By default it's None, but it can be
        #: the name of the subcommand to execute.
        #:
        #: If chaining is enabled this will be set to ``'*'`` in case
        #: any commands are executed.  It is however not possible to
        #: figure out which ones.  If you require this knowledge you
        #: should use a :func:`result_callback`.
        self.invoked_subcommand: str | None = None

        if terminal_width is None and parent is not None:
            terminal_width = parent.terminal_width

        #: The width of the terminal (None is autodetection).
        self.terminal_width: int | None = terminal_width

        if max_content_width is None and parent is not None:
            max_content_width = parent.max_content_width

        #: The maximum width of formatted content (None implies a sensible
        #: default which is 80 for most things).
        self.max_content_width: int | None = max_content_width

        if allow_extra_args is None:
            allow_extra_args = command.allow_extra_args

        #: Indicates if the context allows extra args or if it should
        #: fail on parsing.
        #:
        #: .. versionadded:: 3.0
        self.allow_extra_args = allow_extra_args

        if allow_interspersed_args is None:
            allow_interspersed_args = command.allow_interspersed_args

        #: Indicates if the context allows mixing of arguments and
        #: options or not.
        #:
        #: .. versionadded:: 3.0
        self.allow_interspersed_args: bool = allow_interspersed_args

        if ignore_unknown_options is None:
            ignore_unknown_options = command.ignore_unknown_options

        #: Instructs click to ignore options that a command does not
        #: understand and will store it on the context for later
        #: processing.  This is primarily useful for situations where you
        #: want to call into external programs.  Generally this pattern is
        #: strongly discouraged because it's not possibly to losslessly
        #: forward all arguments.
        #:
        #: .. versionadded:: 4.0
        self.ignore_unknown_options: bool = ignore_unknown_options

        if help_option_names is None:
            if parent is not None:
                help_option_names = parent.help_option_names
            else:
                help_option_names = ["--help"]

        #: The names for the help options.
        self.help_option_names: list[str] = help_option_names

        if token_normalize_func is None and parent is not None:
            token_normalize_func = parent.token_normalize_func

        #: An optional normalization function for tokens.  This is
        #: options, choices, commands etc.
        self.token_normalize_func: t.Callable[[str], str] | None = token_normalize_func

        #: Indicates if resilient parsing is enabled.  In that case Click
        #: will do its best to not cause any failures and default values
        #: will be ignored. Useful for completion.
        self.resilient_parsing: bool = resilient_parsing

        # If there is no envvar prefix yet, but the parent has one and
        # the command on this level has a name, we can expand the envvar
        # prefix automatically.
        if auto_envvar_prefix is None:
            if (
                parent is not None
                and parent.auto_envvar_prefix is not None
                and self.info_name is not None
            ):
                auto_envvar_prefix = (
                    f"{parent.auto_envvar_prefix}_{self.info_name.upper()}"
                )
        else:
            auto_envvar_prefix = auto_envvar_prefix.upper()

        if auto_envvar_prefix is not None:
            auto_envvar_prefix = auto_envvar_prefix.replace("-", "_")

        self.auto_envvar_prefix: str | None = auto_envvar_prefix

        if color is None and parent is not None:
            color = parent.color

        #: Controls if styling output is wanted or not.
        self.color: bool | None = color

        if show_default is None and parent is not None:
            show_default = parent.show_default

        #: Show option default values when formatting help text.
        self.show_default: bool | None = show_default

        self._close_callbacks: list[t.Callable[[], t.Any]] = []
        self._depth = 0
        self._parameter_source: dict[str, ParameterSource] = {}
        # Tracks whether the option that currently owns each parameter slot in
        # :attr:`params` had its ``default`` set explicitly by the user. Used
        # to tie-break feature-switch groups where multiple options share a
        # parameter name and both fall back to their default value.
        # Refs: https://github.com/pallets/click/issues/3403
        self._param_default_explicit: dict[str, bool] = {}
        self._exit_stack = ExitStack()

    @property
    def protected_args(self) -> list[str]:
        import warnings

        warnings.warn(
            "'protected_args' 已被弃用，并将在 Click 9.0 中移除。'args' 将包含其余未解析的标记。",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._protected_args

    def to_info_dict(self) -> dict[str, t.Any]:
        """
        收集可能对生成面向用户的文档工具有用的信息。这将遍历整个命令行界面（CLI）结构

        .. code-block:: python

            with Context(cli) as ctx:
                info = ctx.to_info_dict()

        .. versionadded:: 8.0
        """
        return {
            "command": self.command.to_info_dict(self),
            "info_name": self.info_name,
            "allow_extra_args": self.allow_extra_args,
            "allow_interspersed_args": self.allow_interspersed_args,
            "ignore_unknown_options": self.ignore_unknown_options,
            "auto_envvar_prefix": self.auto_envvar_prefix,
        }

    def __enter__(self) -> Context:
        self._depth += 1
        push_context(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        self._depth -= 1
        exit_result: bool | None = None
        if self._depth == 0:
            exit_result = self._close_with_exception_info(exc_type, exc_value, tb)
        pop_context()

        return exit_result

    @contextmanager
    def scope(self, cleanup: bool = True) -> cabc.Iterator[Context]:
        """This helper method can be used with the context object to promote
        it to the current thread local (see :func:`get_current_context`).
        The default behavior of this is to invoke the cleanup functions which
        can be disabled by setting `cleanup` to `False`.  The cleanup
        functions are typically used for things such as closing file handles.

        If the cleanup is intended the context object can also be directly
        used as a context manager.

        Example usage::

            with ctx.scope():
                assert get_current_context() is ctx

        This is equivalent::

            with ctx:
                assert get_current_context() is ctx

        .. versionadded:: 5.0

        :param cleanup: controls if the cleanup functions should be run or
                        not.  The default is to run these functions.  In
                        some situations the context only wants to be
                        temporarily pushed in which case this can be disabled.
                        Nested pushes automatically defer the cleanup.
        """
        if not cleanup:
            self._depth += 1
        try:
            with self as rv:
                yield rv
        finally:
            if not cleanup:
                self._depth -= 1

    @property
    def meta(self) -> dict[str, t.Any]:
        """
        这是一个与所有嵌套上下文共享的字典。它的存在是为了让点击工具（click utilities）在需要时可以将一些状态存储在这里。
        然而，管理这个字典的责任在于相关代码本身。

        字典的键应该是唯一的点号分隔字符串。例如，模块路径就是一个很好的选择。存储在其中的内容对 click 的运行并不重要。
        但重要的是，将数据放入此处的代码必须遵循系统的通用语义。

        示例用法::

            LANG_KEY = f'{__name__}.lang'

            def set_language(value):
                ctx = get_current_context()
                ctx.meta[LANG_KEY] = value

            def get_language():
                return get_current_context().meta.get(LANG_KEY, 'en_US')

        .. versionadded:: 5.0
        """
        return self._meta

    def make_formatter(self) -> HelpFormatter:
        """
        为帮助和使用输出创建 :class:`~click.HelpFormatter`。

        若要快速自定义所使用的格式化器类而不覆盖此方法，请设置 :attr:`formatter_class` 属性。

        .. versionchanged:: 8.0
            添加了 :attr:`formatter_class` 属性。
        """
        return self.formatter_class(
            width=self.terminal_width, max_width=self.max_content_width
        )

    def with_resource(self, context_manager: AbstractContextManager[V]) -> V:
        """
        将一个资源注册为在 ``with`` 语句中使用。当上下文被弹出时，该资源将被清理。

        使用 :meth:`contextlib.ExitStack.enter_context`。它会调用资源的 ``__enter__()`` 方法并返回结果。
        当上下文被弹出时，它会关闭堆栈，从而调用资源的 ``__exit__()`` 方法。

        要为非上下文管理器对象注册清理函数，请使用 :meth:`call_on_close`。
        或者，可以使用 :mod:`contextlib` 中的某些功能先将其转换为上下文管理器。

        .. code-block:: python

            @click.group()
            @click.option("--name")
            @click.pass_context
            def cli(ctx):
                ctx.obj = ctx.with_resource(connect_db(name))

        :param context_manager: 要进入的上下文管理器

        :return: 无论 ``context_manager.__enter__()`` 返回什么

        .. versionadded:: 8.0
        """
        return self._exit_stack.enter_context(context_manager)

    def call_on_close(self, f: t.Callable[..., t.Any]) -> t.Callable[..., t.Any]:
        """
        注册一个函数，以便在上下文销毁时被调用。

        这可以用于关闭在脚本执行期间打开的资源。
        支持 Python 上下文管理器协议（可在 ``with`` 语句中使用）的资源应使用 :meth:`with_resource` 方法进行注册。

        :param f: 在拆除时执行的功能
        """
        return self._exit_stack.callback(f)

    def close(self) -> None:
        """
        调用所有通过 :meth:`call_on_close` 注册的关闭回调，并退出所有通过 :meth:`with_resource` 进入的上下文管理器。
        """
        self._close_with_exception_info(None, None, None)

    def _close_with_exception_info(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """
        通过调用其 `:meth:`__exit__` 方法并传递异常信息来解除退出栈，以便使用 :meth:`with_resource` 注册的各种资源能够进行异常处理。

        :return: 无论 ``exit_stack.__exit__()`` 返回什么。
        """
        exit_result = self._exit_stack.__exit__(exc_type, exc_value, tb)
        # In case the context is reused, create a new exit stack.
        self._exit_stack = ExitStack()

        return exit_result

    @property
    def command_path(self) -> str:
        """
        计算得到的命令路径。这用于帮助页面上的“用法”信息。它是通过将上下文链到根节点的信息名称组合起来自动创建的。
        """
        rv = ""
        if self.info_name is not None:
            rv = self.info_name
        if self.parent is not None:
            parent_command_path = [self.parent.command_path]

            if isinstance(self.parent.command, Command):
                for param in self.parent.command.get_params(self):
                    parent_command_path.extend(param.get_usage_pieces(self))

            rv = f"{' '.join(parent_command_path)} {rv}"
        return rv.lstrip()

    def find_root(self) -> Context:
        """找到最外层的上下文"""
        node = self
        while node.parent is not None:
            node = node.parent
        return node

    def find_object(self, object_type: type[V]) -> V | None:
        """查找给定类型中最接近的对象"""
        node: Context | None = self

        while node is not None:
            if isinstance(node.obj, object_type):
                return node.obj

            node = node.parent

        return None

    def ensure_object(self, object_type: type[V]) -> V:
        """Like :meth:`find_object` but sets the innermost object to a
        new instance of `object_type` if it does not exist.
        """
        rv = self.find_object(object_type)
        if rv is None:
            self.obj = rv = object_type()
        return rv

    def _default_map_has(self, name: str | None) -> bool:
        """Check if :attr:`default_map` contains a real value for ``name``.

        Returns ``False`` when the key is absent, the map is ``None``,
        ``name`` is ``None``, or the stored value is the internal
        :data:`UNSET` sentinel.
        """
        return (
            name is not None
            and self.default_map is not None
            and name in self.default_map
            and self.default_map[name] is not UNSET
        )

    @t.overload
    def lookup_default(
        self, name: str, call: t.Literal[True] = True
    ) -> t.Any | None: ...

    @t.overload
    def lookup_default(
        self, name: str, call: t.Literal[False] = ...
    ) -> t.Any | t.Callable[[], t.Any] | None: ...

    def lookup_default(self, name: str, call: bool = True) -> t.Any | None:
        """Get the default for a parameter from :attr:`default_map`.

        :param name: Name of the parameter.
        :param call: If the default is a callable, call it. Disable to
            return the callable instead.

        .. versionchanged:: 8.0
            Added the ``call`` parameter.
        """
        if not self._default_map_has(name):
            return None

        # Assert to make the type checker happy.
        assert self.default_map is not None
        value = self.default_map[name]

        if call and callable(value):
            return value()

        return value

    def fail(self, message: str) -> t.NoReturn:
        """Aborts the execution of the program with a specific error
        message.

        :param message: the error message to fail with.
        """
        raise UsageError(message, self)

    def abort(self) -> t.NoReturn:
        """Aborts the script."""
        raise Abort()

    def exit(self, code: int = 0) -> t.NoReturn:
        """Exits the application with a given exit code.

        .. versionchanged:: 8.2
            Callbacks and context managers registered with :meth:`call_on_close`
            and :meth:`with_resource` are closed before exiting.
        """
        self.close()
        raise Exit(code)

    def get_usage(self) -> str:
        """Helper method to get formatted usage string for the current
        context and command.
        """
        return self.command.get_usage(self)

    def get_help(self) -> str:
        """Helper method to get formatted help page for the current
        context and command.
        """
        return self.command.get_help(self)

    def _make_sub_context(self, command: Command) -> Context:
        """Create a new context of the same type as this context, but
        for a new command.

        :meta private:
        """
        return type(self)(command, info_name=command.name, parent=self)

    @t.overload
    def invoke(
        self, callback: t.Callable[..., V], /, *args: t.Any, **kwargs: t.Any
    ) -> V: ...

    @t.overload
    def invoke(self, callback: Command, /, *args: t.Any, **kwargs: t.Any) -> t.Any: ...

    def invoke(
        self, callback: Command | t.Callable[..., V], /, *args: t.Any, **kwargs: t.Any
    ) -> t.Any | V:
        """Invokes a command callback in exactly the way it expects.  There
        are two ways to invoke this method:

        1.  the first argument can be a callback and all other arguments and
            keyword arguments are forwarded directly to the function.
        2.  the first argument is a click command object.  In that case all
            arguments are forwarded as well but proper click parameters
            (options and click arguments) must be keyword arguments and Click
            will fill in defaults.

        .. versionchanged:: 8.0
            All ``kwargs`` are tracked in :attr:`params` so they will be
            passed if :meth:`forward` is called at multiple levels.

        .. versionchanged:: 3.2
            A new context is created, and missing arguments use default values.
        """
        if isinstance(callback, Command):
            other_cmd = callback

            if other_cmd.callback is None:
                raise TypeError(
                    "The given command does not have a callback that can be invoked."
                )
            else:
                callback = t.cast("t.Callable[..., V]", other_cmd.callback)

            ctx = self._make_sub_context(other_cmd)

            for param in other_cmd.params:
                if param.name not in kwargs and param.expose_value:
                    default_value = param.get_default(ctx)
                    # We explicitly hide the :attr:`UNSET` value to the user, as we
                    # choose to make it an implementation detail. And because ``invoke``
                    # has been designed as part of Click public API, we return ``None``
                    # instead. Refs:
                    # https://github.com/pallets/click/issues/3066
                    # https://github.com/pallets/click/issues/3065
                    # https://github.com/pallets/click/pull/3068
                    if default_value is UNSET:
                        default_value = None
                    kwargs[param.name] = param.type_cast_value(ctx, default_value)

            # Track all kwargs as params, so that forward() will pass
            # them on in subsequent calls.
            ctx.params.update(kwargs)
        else:
            ctx = self

        with augment_usage_errors(self):
            with ctx:
                return callback(*args, **kwargs)

    def forward(self, cmd: Command, /, *args: t.Any, **kwargs: t.Any) -> t.Any:
        """Similar to :meth:`invoke` but fills in default keyword
        arguments from the current context if the other command expects
        it.  This cannot invoke callbacks directly, only other commands.

        .. versionchanged:: 8.0
            All ``kwargs`` are tracked in :attr:`params` so they will be
            passed if ``forward`` is called at multiple levels.
        """
        # Can only forward to other commands, not direct callbacks.
        if not isinstance(cmd, Command):
            raise TypeError("回调不是一条命令")

        for param in self.params:
            if param not in kwargs:
                kwargs[param] = self.params[param]

        return self.invoke(cmd, *args, **kwargs)

    def set_parameter_source(self, name: str, source: ParameterSource) -> None:
        """Set the source of a parameter. This indicates the location
        from which the value of the parameter was obtained.

        :param name: The name of the parameter.
        :param source: A member of :class:`~click.core.ParameterSource`.
        """
        self._parameter_source[name] = source

    def get_parameter_source(self, name: str) -> ParameterSource | None:
        """Get the source of a parameter. This indicates the location
        from which the value of the parameter was obtained.

        This can be useful for determining when a user specified a value
        on the command line that is the same as the default value. It
        will be :attr:`~click.core.ParameterSource.DEFAULT` only if the
        value was actually taken from the default.

        :param name: The name of the parameter.
        :rtype: ParameterSource

        .. versionchanged:: 8.0
            Returns ``None`` if the parameter was not provided from any
            source.
        """
        return self._parameter_source.get(name)


class Command:
    """Commands are the basic building block of command line interfaces in
    Click.  A basic command handles command line parsing and might dispatch
    more parsing to commands nested below it.

    :param name: the name of the command to use unless a group overrides it.
    :param context_settings: an optional dictionary with defaults that are
                             passed to the context object.
    :param callback: the callback to invoke.  This is optional.
    :param params: the parameters to register with this command.  This can
                   be either :class:`Option` or :class:`Argument` objects.
    :param help: the help string to use for this command.
    :param epilog: like the help string but it's printed at the end of the
                   help page after everything else.
    :param short_help: the short help to use for this command.  This is
                       shown on the command listing of the parent command.
    :param add_help_option: by default each command registers a ``--help``
                            option.  This can be disabled by this parameter.
    :param no_args_is_help: this controls what happens if no arguments are
                            provided.  This option is disabled by default.
                            If enabled this will add ``--help`` as argument
                            if no arguments are passed
    :param hidden: hide this command from help outputs.
    :param deprecated: If ``True`` or non-empty string, issues a message
                        indicating that the command is deprecated and highlights
                        its deprecation in --help. The message can be customized
                        by using a string as the value.

    .. versionchanged:: 8.2
        This is the base class for all commands, not ``BaseCommand``.
        ``deprecated`` can be set to a string as well to customize the
        deprecation message.

    .. versionchanged:: 8.1
        ``help``, ``epilog``, and ``short_help`` are stored unprocessed,
        all formatting is done when outputting help text, not at init,
        and is done even if not using the ``@command`` decorator.

    .. versionchanged:: 8.0
        Added a ``repr`` showing the command name.

    .. versionchanged:: 7.1
        Added the ``no_args_is_help`` parameter.

    .. versionchanged:: 2.0
        Added the ``context_settings`` parameter.
    """

    #: The context class to create with :meth:`make_context`.
    #:
    #: .. versionadded:: 8.0
    context_class: type[Context] = Context

    #: the default for the :attr:`Context.allow_extra_args` flag.
    allow_extra_args = False

    #: the default for the :attr:`Context.allow_interspersed_args` flag.
    allow_interspersed_args = True

    #: the default for the :attr:`Context.ignore_unknown_options` flag.
    ignore_unknown_options = False

    def __init__(
        self,
        name: str | None,
        context_settings: cabc.MutableMapping[str, t.Any] | None = None,
        callback: t.Callable[..., t.Any] | None = None,
        params: list[Parameter] | None = None,
        help: str | None = None,
        epilog: str | None = None,
        short_help: str | None = None,
        options_metavar: str | None = "[选项]",
        add_help_option: bool = True,
        no_args_is_help: bool = False,
        hidden: bool = False,
        deprecated: bool | str = False,
    ) -> None:
        #: the name the command thinks it has.  Upon registering a command
        #: on a :class:`Group` the group will default the command name
        #: with this information.  You should instead use the
        #: :class:`Context`\'s :attr:`~Context.info_name` attribute.
        self.name = name

        if context_settings is None:
            context_settings = {}

        #: an optional dictionary with defaults passed to the context.
        self.context_settings: cabc.MutableMapping[str, t.Any] = context_settings

        #: the callback to execute when the command fires.  This might be
        #: `None` in which case nothing happens.
        self.callback = callback
        #: the list of parameters for this command in the order they
        #: should show up in the help page and execute.  Eager parameters
        #: will automatically be handled before non eager ones.
        self.params: list[Parameter] = params or []
        self.help = help
        self.epilog = epilog
        self.options_metavar = options_metavar
        self.short_help = short_help
        self.add_help_option = add_help_option
        self._help_option = None
        self.no_args_is_help = no_args_is_help
        self.hidden = hidden
        self.deprecated = deprecated

    def to_info_dict(self, ctx: Context) -> dict[str, t.Any]:
        return {
            "name": self.name,
            "params": [param.to_info_dict() for param in self.get_params(ctx)],
            "help": self.help,
            "epilog": self.epilog,
            "short_help": self.short_help,
            "hidden": self.hidden,
            "deprecated": self.deprecated,
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.name}>"

    def get_usage(self, ctx: Context) -> str:
        """Formats the usage line into a string and returns it.

        Calls :meth:`format_usage` internally.
        """
        formatter = ctx.make_formatter()
        self.format_usage(ctx, formatter)
        return formatter.getvalue().rstrip("\n")

    def get_params(self, ctx: Context) -> list[Parameter]:
        params = self.params
        help_option = self.get_help_option(ctx)

        if help_option is not None:
            params = [*params, help_option]

        if __debug__:
            import warnings

            opts = [opt for param in params for opt in param.opts]
            opts_counter = Counter(opts)
            duplicate_opts = (opt for opt, count in opts_counter.items() if count > 1)

            for duplicate_opt in duplicate_opts:
                warnings.warn(
                    (
                        f"参数 {duplicate_opt} 被使用了多次。请删除其重复项，因为参数应保持唯一性。"
                    ),
                    stacklevel=3,
                )

        return params

    def format_usage(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Writes the usage line into the formatter.

        This is a low-level method called by :meth:`get_usage`.
        """
        pieces = self.collect_usage_pieces(ctx)
        formatter.write_usage(ctx.command_path, " ".join(pieces))

    def collect_usage_pieces(self, ctx: Context) -> list[str]:
        """Returns all the pieces that go into the usage line and returns
        it as a list of strings.
        """
        rv = [self.options_metavar] if self.options_metavar else []

        for param in self.get_params(ctx):
            rv.extend(param.get_usage_pieces(ctx))

        return rv

    def get_help_option_names(self, ctx: Context) -> list[str]:
        """Returns the names for the help option."""
        all_names = set(ctx.help_option_names)
        for param in self.params:
            all_names.difference_update(param.opts)
            all_names.difference_update(param.secondary_opts)
        return list(all_names)

    def get_help_option(self, ctx: Context) -> Option | None:
        """Returns the help option object.

        Skipped if :attr:`add_help_option` is ``False``.

        .. versionchanged:: 8.1.8
            The help option is now cached to avoid creating it multiple times.
        """
        help_option_names = self.get_help_option_names(ctx)

        if not help_option_names or not self.add_help_option:
            return None

        # Cache the help option object in private _help_option attribute to
        # avoid creating it multiple times. Not doing this will break the
        # callback ordering by iter_params_for_processing(), which relies on
        # object comparison.
        if self._help_option is None:
            # Avoid circular import.
            from .decorators import help_option

            # Apply help_option decorator and pop resulting option
            help_option(*help_option_names)(self)
            self._help_option = self.params.pop()  # type: ignore[assignment]

        return self._help_option

    def make_parser(self, ctx: Context) -> _OptionParser:
        """Creates the underlying option parser for this command."""
        parser = _OptionParser(ctx)
        for param in self.get_params(ctx):
            param.add_to_parser(parser, ctx)
        return parser

    def get_help(self, ctx: Context) -> str:
        """Formats the help into a string and returns it.

        Calls :meth:`format_help` internally.
        """
        formatter = ctx.make_formatter()
        self.format_help(ctx, formatter)
        return formatter.getvalue().rstrip("\n")

    def get_short_help_str(self, limit: int = 45) -> str:
        """Gets short help for the command or makes it by shortening the
        long help string.
        """
        if self.short_help:
            text = inspect.cleandoc(self.short_help)
        elif self.help:
            text = make_default_short_help(self.help, limit)
        else:
            text = ""

        if self.deprecated:
            text = f"{_(text)} {_format_deprecated_label(self.deprecated)}"

        return text.strip()

    def format_help(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Writes the help into the formatter if it exists.

        This is a low-level method called by :meth:`get_help`.

        This calls the following methods:

        -   :meth:`format_usage`
        -   :meth:`format_help_text`
        -   :meth:`format_options`
        -   :meth:`format_epilog`
        """
        self.format_usage(ctx, formatter)
        self.format_help_text(ctx, formatter)
        self.format_options(ctx, formatter)
        self.format_epilog(ctx, formatter)

    def format_help_text(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Writes the help text to the formatter if it exists."""
        if self.help is not None:
            # truncate the help text to the first form feed
            text = inspect.cleandoc(self.help).partition("\f")[0]
        else:
            text = ""

        if self.deprecated:
            text = f"{_(text)} {_format_deprecated_label(self.deprecated)}"

        if text:
            formatter.write_paragraph()

            with formatter.indentation():
                formatter.write_text(text)

    def format_options(self, ctx: Context, formatter: HelpFormatter) -> None:
        """将所有选项写入格式化器（如果它们存在）中"""
        opts = []
        for param in self.get_params(ctx):
            rv = param.get_help_record(ctx)
            if rv is not None:
                opts.append(rv)

        if opts:
            with formatter.section(_("选项")):
                formatter.write_dl(opts)

    def format_epilog(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Writes the epilog into the formatter if it exists."""
        if self.epilog:
            epilog = inspect.cleandoc(self.epilog)
            formatter.write_paragraph()

            with formatter.indentation():
                formatter.write_text(epilog)

    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: Context | None = None,
        **extra: t.Any,
    ) -> Context:
        """This function when given an info name and arguments will kick
        off the parsing and create a new :class:`Context`.  It does not
        invoke the actual command callback though.

        To quickly customize the context class used without overriding
        this method, set the :attr:`context_class` attribute.

        :param info_name: the info name for this invocation.  Generally this
                          is the most descriptive name for the script or
                          command.  For the toplevel script it's usually
                          the name of the script, for commands below it's
                          the name of the command.
        :param args: the arguments to parse as list of strings.
        :param parent: the parent context if available.
        :param extra: extra keyword arguments forwarded to the context
                      constructor.

        .. versionchanged:: 8.0
            Added the :attr:`context_class` attribute.
        """
        for key, value in self.context_settings.items():
            if key not in extra:
                extra[key] = value

        ctx = self.context_class(self, info_name=info_name, parent=parent, **extra)

        with ctx.scope(cleanup=False):
            self.parse_args(ctx, args)
        return ctx

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        if not args and self.no_args_is_help and not ctx.resilient_parsing:
            raise NoArgsIsHelpError(ctx)

        parser = self.make_parser(ctx)
        opts, args, param_order = parser.parse_args(args=args)

        for param in iter_params_for_processing(param_order, self.get_params(ctx)):
            _, args = param.handle_parse_result(ctx, opts, args)

        # We now have all parameters' values into `ctx.params`, but the data may contain
        # the `UNSET` sentinel.
        # Convert `UNSET` to `None` to ensure that the user doesn't see `UNSET`.
        #
        # Waiting until after the initial parse to convert allows us to treat `UNSET`
        # more like a missing value when multiple params use the same name.
        # Refs:
        # https://github.com/pallets/click/issues/3071
        # https://github.com/pallets/click/pull/3079
        for name, value in ctx.params.items():
            if value is UNSET:
                ctx.params[name] = None

        if args and not ctx.allow_extra_args and not ctx.resilient_parsing:
            ctx.fail(
                ngettext(
                    "遇到意外的额外参数 ({args})",
                    "遇到意外的额外参数 ({args})",
                    len(args),
                ).format(args=" ".join(map(str, args)))
            )

        ctx.args = args
        ctx._opt_prefixes.update(parser._opt_prefixes)
        return args

    def invoke(self, ctx: Context) -> t.Any:
        """Given a context, this invokes the attached callback (if it exists)
        in the right way.
        """
        if self.deprecated:
            message = _(
                "弃用警告: 命令 {name!r} 已被弃用。{extra_message}"
            ).format(
                name=self.name,
                extra_message=_format_deprecated_suffix(self.deprecated),
            )
            echo(style(message, fg="red"), err=True)

        if self.callback is not None:
            return ctx.invoke(self.callback, **ctx.params)

    def shell_complete(self, ctx: Context, incomplete: str) -> list[CompletionItem]:
        """Return a list of completions for the incomplete value. Looks
        at the names of options and chained multi-commands.

        Any command could be part of a chained multi-command, so sibling
        commands are valid at any point during command completion.

        :param ctx: Invocation context for this command.
        :param incomplete: Value being completed. May be empty.

        .. versionadded:: 8.0
        """
        from click.shell_completion import CompletionItem

        results: list[CompletionItem] = []

        if incomplete and not incomplete[0].isalnum():
            for param in self.get_params(ctx):
                if (
                    not isinstance(param, Option)
                    or param.hidden
                    or (
                        not param.multiple
                        and ctx.get_parameter_source(param.name)
                        is ParameterSource.COMMANDLINE
                    )
                ):
                    continue

                results.extend(
                    CompletionItem(name, help=param.help)
                    for name in [*param.opts, *param.secondary_opts]
                    if name.startswith(incomplete)
                )

        while ctx.parent is not None:
            ctx = ctx.parent

            if isinstance(ctx.command, Group) and ctx.command.chain:
                results.extend(
                    CompletionItem(name, help=command.get_short_help_str())
                    for name, command in _complete_visible_commands(ctx, incomplete)
                    if name not in ctx._protected_args
                )

        return results

    @t.overload
    def main(
        self,
        args: cabc.Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: t.Literal[True] = True,
        **extra: t.Any,
    ) -> t.NoReturn: ...

    @t.overload
    def main(
        self,
        args: cabc.Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = ...,
        **extra: t.Any,
    ) -> t.Any: ...

    def main(
        self,
        args: cabc.Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: t.Any,
    ) -> t.Any:
        """This is the way to invoke a script with all the bells and
        whistles as a command line application.  This will always terminate
        the application after a call.  If this is not wanted, ``SystemExit``
        needs to be caught.

        This method is also available by directly calling the instance of
        a :class:`Command`.

        :param args: the arguments that should be used for parsing.  If not
                     provided, ``sys.argv[1:]`` is used.
        :param prog_name: the program name that should be used.  By default
                          the program name is constructed by taking the file
                          name from ``sys.argv[0]``.
        :param complete_var: the environment variable that controls the
                             bash completion support.  The default is
                             ``"_<prog_name>_COMPLETE"`` with prog_name in
                             uppercase.
        :param standalone_mode: the default behavior is to invoke the script
                                in standalone mode.  Click will then
                                handle exceptions and convert them into
                                error messages and the function will never
                                return but shut down the interpreter.  If
                                this is set to `False` they will be
                                propagated to the caller and the return
                                value of this function is the return value
                                of :meth:`invoke`.
        :param windows_expand_args: Expand glob patterns, user dir, and
            env vars in command line args on Windows.
        :param extra: extra keyword arguments are forwarded to the context
                      constructor.  See :class:`Context` for more information.

        .. versionchanged:: 8.0.1
            Added the ``windows_expand_args`` parameter to allow
            disabling command line arg expansion on Windows.

        .. versionchanged:: 8.0
            When taking arguments from ``sys.argv`` on Windows, glob
            patterns, user dir, and env vars are expanded.

        .. versionchanged:: 3.0
           Added the ``standalone_mode`` parameter.
        """
        if args is None:
            args = sys.argv[1:]

            if os.name == "nt" and windows_expand_args:
                args = _expand_args(args)
        else:
            args = list(args)

        if prog_name is None:
            prog_name = _detect_program_name()

        # Process shell completion requests and exit early.
        self._main_shell_completion(extra, prog_name, complete_var)

        try:
            try:
                with self.make_context(prog_name, args, **extra) as ctx:
                    rv = self.invoke(ctx)
                    if not standalone_mode:
                        return rv
                    # it's not safe to `ctx.exit(rv)` here!
                    # note that `rv` may actually contain data like "1" which
                    # has obvious effects
                    # more subtle case: `rv=[None, None]` can come out of
                    # chained commands which all returned `None` -- so it's not
                    # even always obvious that `rv` indicates success/failure
                    # by its truthiness/falsiness
                    ctx.exit()
            except (EOFError, KeyboardInterrupt) as e:
                echo(file=sys.stderr)
                raise Abort() from e
            except ClickException as e:
                if not standalone_mode:
                    raise
                e.show()
                sys.exit(e.exit_code)
            except OSError as e:
                if e.errno == errno.EPIPE:
                    sys.stdout = t.cast(t.TextIO, PacifyFlushWrapper(sys.stdout))
                    sys.stderr = t.cast(t.TextIO, PacifyFlushWrapper(sys.stderr))
                    sys.exit(1)
                else:
                    raise
        except Exit as e:
            if standalone_mode:
                sys.exit(e.exit_code)
            else:
                # in non-standalone mode, return the exit code
                # note that this is only reached if `self.invoke` above raises
                # an Exit explicitly -- thus bypassing the check there which
                # would return its result
                # the results of non-standalone execution may therefore be
                # somewhat ambiguous: if there are codepaths which lead to
                # `ctx.exit(1)` and to `return 1`, the caller won't be able to
                # tell the difference between the two
                return e.exit_code
        except Abort:
            if not standalone_mode:
                raise
            echo(_("中止!"), file=sys.stderr)
            sys.exit(1)

    def _main_shell_completion(
        self,
        ctx_args: cabc.MutableMapping[str, t.Any],
        prog_name: str,
        complete_var: str | None = None,
    ) -> None:
        """Check if the shell is asking for tab completion, process
        that, then exit early. Called from :meth:`main` before the
        program is invoked.

        :param prog_name: Name of the executable in the shell.
        :param complete_var: Name of the environment variable that holds
            the completion instruction. Defaults to
            ``_{PROG_NAME}_COMPLETE``.

        .. versionchanged:: 8.2.0
            Dots (``.``) in ``prog_name`` are replaced with underscores (``_``).
        """
        if complete_var is None:
            complete_name = prog_name.replace("-", "_").replace(".", "_")
            complete_var = f"_{complete_name}_COMPLETE".upper()

        instruction = os.environ.get(complete_var)

        if not instruction:
            return

        from .shell_completion import shell_complete

        rv = shell_complete(self, ctx_args, prog_name, complete_var, instruction)
        sys.exit(rv)

    def __call__(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
        """:meth:`main` 的别名"""
        return self.main(*args, **kwargs)


class _FakeSubclassCheck(type):
    def __subclasscheck__(cls, subclass: type) -> bool:
        return issubclass(subclass, cls.__bases__[0])

    def __instancecheck__(cls, instance: t.Any) -> bool:
        return isinstance(instance, cls.__bases__[0])


class _BaseCommand(Command, metaclass=_FakeSubclassCheck):
    """
    .. deprecated:: 8.2
        Will be removed in Click 9.0. Use ``Command`` instead.
    """


class Group(Command):
    """
    组是一个嵌套其他命令（或更多组）的命令

    :param name: 该组命令的名称
    :param commands: 将映射名称与 :class:`Command` 对象关联。可以是一个列表，此时将使用 :attr:`Command.name` 作为键
    :param invoke_without_command: 即使未给出子命令，也调用该组的回调函数
    :param no_args_is_help: 如果未给出参数，则显示该组的帮助信息并退出。默认值与 ``invoke_without_command`` 的相反值相同
    :param subcommand_metavar: 在帮助信息中如何表示子命令参数。默认值将表示是否设置了 ``chain``
    :param chain: 允许传递多个子命令参数。在解析命令参数后，如果仍有剩余参数，则会匹配另一个命令，依此类推
    :param result_callback: 在组命令和子命令的回调函数之后调用的函数。子命令返回的值会被传递。如果启用了 ``chain``，该值将是一个包含所有命令返回值的列表。如果启用了 ``invoke_without_command``，该值将是组命令回调函数返回的值，或者如果启用了 ``chain``，则为空列表
    :param kwargs: 传递给 :class:`Command` 的其他参数

    .. versionchanged:: 8.0
        “commands”参数可以是一个命令对象列表

    .. versionchanged:: 8.2
        与“MultiCommand”基类合并并替换它
    """

    allow_extra_args = True
    allow_interspersed_args = False

    #: If set, this is used by the group's :meth:`command` decorator
    #: as the default :class:`Command` class. This is useful to make all
    #: subcommands use a custom command class.
    #:
    #: .. versionadded:: 8.0
    command_class: type[Command] | None = None

    #: If set, this is used by the group's :meth:`group` decorator
    #: as the default :class:`Group` class. This is useful to make all
    #: subgroups use a custom group class.
    #:
    #: If set to the special value :class:`type` (literally
    #: ``group_class = type``), this group's class will be used as the
    #: default class. This makes a custom group class continue to make
    #: custom groups.
    #:
    #: .. versionadded:: 8.0
    group_class: type[Group] | type[type] | None = None
    # Literal[type] isn't valid, so use Type[type]

    def __init__(
        self,
        name: str | None = None,
        commands: cabc.MutableMapping[str, Command]
        | cabc.Sequence[Command]
        | None = None,
        invoke_without_command: bool = False,
        no_args_is_help: bool | None = None,
        subcommand_metavar: str | None = None,
        chain: bool = False,
        result_callback: t.Callable[..., t.Any] | None = None,
        **kwargs: t.Any,
    ) -> None:
        super().__init__(name, **kwargs)

        if commands is None:
            commands = {}
        elif isinstance(commands, abc.Sequence):
            commands = {c.name: c for c in commands if c.name is not None}

        #: The registered subcommands by their exported names.
        self.commands: cabc.MutableMapping[str, Command] = commands

        if no_args_is_help is None:
            no_args_is_help = not invoke_without_command

        self.no_args_is_help = no_args_is_help
        self.invoke_without_command = invoke_without_command

        if subcommand_metavar is None:
            if chain:
                subcommand_metavar = "命令1 [参数]... [命令2 [参数]...]..."
            else:
                subcommand_metavar = "命令 [参数]..."

        self.subcommand_metavar = subcommand_metavar
        self.chain = chain
        # The result callback that is stored. This can be set or
        # overridden with the :func:`result_callback` decorator.
        self._result_callback = result_callback

        if self.chain:
            for param in self.params:
                if isinstance(param, Argument) and not param.required:
                    raise RuntimeError(
                        "链式模式中的一组不能包含可选参数"
                    )

    def to_info_dict(self, ctx: Context) -> dict[str, t.Any]:
        info_dict = super().to_info_dict(ctx)
        commands = {}

        for name in self.list_commands(ctx):
            command = self.get_command(ctx, name)

            if command is None:
                continue

            sub_ctx = ctx._make_sub_context(command)

            with sub_ctx.scope(cleanup=False):
                commands[name] = command.to_info_dict(sub_ctx)

        info_dict.update(commands=commands, chain=self.chain)
        return info_dict

    def add_command(self, cmd: Command, name: str | None = None) -> None:
        """Registers another :class:`Command` with this group.  If the name
        is not provided, the name of the command is used.
        """
        name = name or cmd.name
        if name is None:
            raise TypeError("命令没有名称")
        _check_nested_chain(self, name, cmd, register=True)
        self.commands[name] = cmd

    @t.overload
    def command(self, __func: t.Callable[..., t.Any]) -> Command: ...

    @t.overload
    def command(
        self, *args: t.Any, **kwargs: t.Any
    ) -> t.Callable[[t.Callable[..., t.Any]], Command]: ...

    def command(
        self, *args: t.Any, **kwargs: t.Any
    ) -> t.Callable[[t.Callable[..., t.Any]], Command] | Command:
        """
        一个用于声明并将命令附加到组的快捷装饰器。

        它接受与 :func:`command` 相同的参数，并通过调用 :meth:`add_command` 立即将创建的命令注册到该组中。

        若要自定义所使用的命令类，请设置 :attr:`command_class` 属性。

        .. versionchanged:: 8.1
            此装饰器可以不带括号直接使用。

        .. versionchanged:: 8.0
            新增了 :attr:`command_class` 属性。
        """
        from .decorators import command

        func: t.Callable[..., t.Any] | None = None

        if args and callable(args[0]):
            assert len(args) == 1 and not kwargs, (
                "使用 'command(**kwargs)(callable)' 提供参数"
            )
            (func,) = args
            args = ()

        if self.command_class and kwargs.get("cls") is None:
            kwargs["cls"] = self.command_class

        def decorator(f: t.Callable[..., t.Any]) -> Command:
            cmd: Command = command(*args, **kwargs)(f)
            self.add_command(cmd)
            return cmd

        if func is not None:
            return decorator(func)

        return decorator

    @t.overload
    def group(self, __func: t.Callable[..., t.Any]) -> Group: ...

    @t.overload
    def group(
        self, *args: t.Any, **kwargs: t.Any
    ) -> t.Callable[[t.Callable[..., t.Any]], Group]: ...

    def group(
        self, *args: t.Any, **kwargs: t.Any
    ) -> t.Callable[[t.Callable[..., t.Any]], Group] | Group:
        """A shortcut decorator for declaring and attaching a group to
        the group. This takes the same arguments as :func:`group` and
        immediately registers the created group with this group by
        calling :meth:`add_command`.

        To customize the group class used, set the :attr:`group_class`
        attribute.

        .. versionchanged:: 8.1
            This decorator can be applied without parentheses.

        .. versionchanged:: 8.0
            Added the :attr:`group_class` attribute.
        """
        from .decorators import group

        func: t.Callable[..., t.Any] | None = None

        if args and callable(args[0]):
            assert len(args) == 1 and not kwargs, (
                "使用 'group(**kwargs)(callable)' 提供参数"
            )
            (func,) = args
            args = ()

        if self.group_class is not None and kwargs.get("cls") is None:
            if self.group_class is type:
                kwargs["cls"] = type(self)
            else:
                kwargs["cls"] = self.group_class

        def decorator(f: t.Callable[..., t.Any]) -> Group:
            cmd: Group = group(*args, **kwargs)(f)
            self.add_command(cmd)
            return cmd

        if func is not None:
            return decorator(func)

        return decorator

    def result_callback(self, replace: bool = False) -> t.Callable[[F], F]:
        """Adds a result callback to the command.  By default if a
        result callback is already registered this will chain them but
        this can be disabled with the `replace` parameter.  The result
        callback is invoked with the return value of the subcommand
        (or the list of return values from all subcommands if chaining
        is enabled) as well as the parameters as they would be passed
        to the main callback.

        Example::

            @click.group()
            @click.option('-i', '--input', default=23)
            def cli(input):
                return 42

            @cli.result_callback()
            def process_result(result, input):
                return result + input

        :param replace: if set to `True` an already existing result
                        callback will be removed.

        .. versionchanged:: 8.0
            Renamed from ``resultcallback``.

        .. versionadded:: 3.0
        """

        def decorator(f: F) -> F:
            old_callback = self._result_callback

            if old_callback is None or replace:
                self._result_callback = f
                return f

            def function(value: t.Any, /, *args: t.Any, **kwargs: t.Any) -> t.Any:
                inner = old_callback(value, *args, **kwargs)
                return f(inner, *args, **kwargs)

            self._result_callback = rv = update_wrapper(t.cast(F, function), f)
            return rv  # type: ignore[return-value]

        return decorator

    def get_command(self, ctx: Context, cmd_name: str) -> Command | None:
        """Given a context and a command name, this returns a :class:`Command`
        object if it exists or returns ``None``.
        """
        return self.commands.get(cmd_name)

    def list_commands(self, ctx: Context) -> list[str]:
        """Returns a list of subcommand names in the order they should appear."""
        return sorted(self.commands)

    def collect_usage_pieces(self, ctx: Context) -> list[str]:
        rv = super().collect_usage_pieces(ctx)
        rv.append(self.subcommand_metavar)
        return rv

    def format_options(self, ctx: Context, formatter: HelpFormatter) -> None:
        super().format_options(ctx, formatter)
        self.format_commands(ctx, formatter)

    def format_commands(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Extra format methods for multi methods that adds all the commands
        after the options.
        """
        commands = []
        for subcommand in self.list_commands(ctx):
            cmd = self.get_command(ctx, subcommand)
            # What is this, the tool lied about a command.  Ignore it
            if cmd is None:
                continue
            if cmd.hidden:
                continue

            commands.append((subcommand, cmd))

        # allow for 3 times the default spacing
        if len(commands):
            limit = formatter.width - 6 - max(len(cmd[0]) for cmd in commands)

            rows = []
            for subcommand, cmd in commands:
                help = cmd.get_short_help_str(limit)
                rows.append((subcommand, help))

            if rows:
                with formatter.section(_("命令")):
                    formatter.write_dl(rows)

    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        if not args and self.no_args_is_help and not ctx.resilient_parsing:
            raise NoArgsIsHelpError(ctx)

        rest = super().parse_args(ctx, args)

        if self.chain:
            ctx._protected_args = rest
            ctx.args = []
        elif rest:
            ctx._protected_args, ctx.args = rest[:1], rest[1:]

        return ctx.args

    def invoke(self, ctx: Context) -> t.Any:
        def _process_result(value: t.Any) -> t.Any:
            if self._result_callback is not None:
                value = ctx.invoke(self._result_callback, value, **ctx.params)
            return value

        if not ctx._protected_args:
            if self.invoke_without_command:
                # No subcommand was invoked, so the result callback is
                # invoked with the group return value for regular
                # groups, or an empty list for chained groups.
                with ctx:
                    rv = super().invoke(ctx)
                    return _process_result([] if self.chain else rv)
            ctx.fail(_("缺少命令"))

        # Fetch args back out
        args = [*ctx._protected_args, *ctx.args]
        ctx.args = []
        ctx._protected_args = []

        # If we're not in chain mode, we only allow the invocation of a
        # single command but we also inform the current context about the
        # name of the command to invoke.
        if not self.chain:
            # Make sure the context is entered so we do not clean up
            # resources until the result processor has worked.
            with ctx:
                cmd_name, cmd, args = self.resolve_command(ctx, args)
                assert cmd is not None
                ctx.invoked_subcommand = cmd_name
                super().invoke(ctx)
                sub_ctx = cmd.make_context(cmd_name, args, parent=ctx)
                with sub_ctx:
                    return _process_result(sub_ctx.command.invoke(sub_ctx))

        # In chain mode we create the contexts step by step, but after the
        # base command has been invoked.  Because at that point we do not
        # know the subcommands yet, the invoked subcommand attribute is
        # set to ``*`` to inform the command that subcommands are executed
        # but nothing else.
        with ctx:
            ctx.invoked_subcommand = "*" if args else None
            super().invoke(ctx)

            # Otherwise we make every single context and invoke them in a
            # chain.  In that case the return value to the result processor
            # is the list of all invoked subcommand's results.
            contexts = []
            while args:
                cmd_name, cmd, args = self.resolve_command(ctx, args)
                assert cmd is not None
                sub_ctx = cmd.make_context(
                    cmd_name,
                    args,
                    parent=ctx,
                    allow_extra_args=True,
                    allow_interspersed_args=False,
                )
                contexts.append(sub_ctx)
                args, sub_ctx.args = sub_ctx.args, []

            rv = []
            for sub_ctx in contexts:
                with sub_ctx:
                    rv.append(sub_ctx.command.invoke(sub_ctx))
            return _process_result(rv)

    def resolve_command(
        self, ctx: Context, args: list[str]
    ) -> tuple[str | None, Command | None, list[str]]:
        cmd_name = make_str(args[0])

        # Get the command
        cmd = self.get_command(ctx, cmd_name)

        # If we can't find the command but there is a normalization
        # function available, we try with that one.
        if cmd is None and ctx.token_normalize_func is not None:
            cmd_name = ctx.token_normalize_func(cmd_name)
            cmd = self.get_command(ctx, cmd_name)

        # If we don't find the command we want to show an error message
        # to the user that it was not provided.  However, there is
        # something else we should do: if the first argument looks like
        # an option we want to kick off parsing again for arguments to
        # resolve things like --help which now should go to the main
        # place.
        if cmd is None and not ctx.resilient_parsing:
            if _split_opt(cmd_name)[0]:
                self.parse_args(ctx, args)
            raise NoSuchCommand(cmd_name, possibilities=self.commands, ctx=ctx)
        return cmd_name if cmd else None, cmd, args[1:]

    def shell_complete(self, ctx: Context, incomplete: str) -> list[CompletionItem]:
        """Return a list of completions for the incomplete value. Looks
        at the names of options, subcommands, and chained
        multi-commands.

        :param ctx: Invocation context for this command.
        :param incomplete: Value being completed. May be empty.

        .. versionadded:: 8.0
        """
        from click.shell_completion import CompletionItem

        results = [
            CompletionItem(name, help=command.get_short_help_str())
            for name, command in _complete_visible_commands(ctx, incomplete)
        ]
        results.extend(super().shell_complete(ctx, incomplete))
        return results


class _MultiCommand(Group, metaclass=_FakeSubclassCheck):
    """
    .. deprecated:: 8.2
        Will be removed in Click 9.0. Use ``Group`` instead.
    """


class CommandCollection(Group):
    """A :class:`Group` that looks up subcommands on other groups. If a command
    is not found on this group, each registered source is checked in order.
    Parameters on a source are not added to this group, and a source's callback
    is not invoked when invoking its commands. In other words, this "flattens"
    commands in many groups into this one group.

    :param name: The name of the group command.
    :param sources: A list of :class:`Group` objects to look up commands from.
    :param kwargs: Other arguments passed to :class:`Group`.

    .. versionchanged:: 8.2
        This is a subclass of ``Group``. Commands are looked up first on this
        group, then each of its sources.
    """

    def __init__(
        self,
        name: str | None = None,
        sources: list[Group] | None = None,
        **kwargs: t.Any,
    ) -> None:
        super().__init__(name, **kwargs)
        #: The list of registered groups.
        self.sources: list[Group] = sources or []

    def add_source(self, group: Group) -> None:
        """将一个组添加为命令的来源"""
        self.sources.append(group)

    def get_command(self, ctx: Context, cmd_name: str) -> Command | None:
        rv = super().get_command(ctx, cmd_name)

        if rv is not None:
            return rv

        for source in self.sources:
            rv = source.get_command(ctx, cmd_name)

            if rv is not None:
                if self.chain:
                    _check_nested_chain(self, cmd_name, rv)

                return rv

        return None

    def list_commands(self, ctx: Context) -> list[str]:
        rv: set[str] = set(super().list_commands(ctx))

        for source in self.sources:
            rv.update(source.list_commands(ctx))

        return sorted(rv)


def _check_iter(value: t.Any) -> cabc.Iterator[t.Any]:
    """Check if the value is iterable but not a string. Raises a type
    error, or return an iterator over the value.
    """
    if isinstance(value, str):
        raise TypeError

    return iter(value)


class Parameter(ABC):
    r"""A parameter to a command comes in two versions: they are either
    :class:`Option`\s or :class:`Argument`\s.  Other subclasses are currently
    not supported by design as some of the internals for parsing are
    intentionally not finalized.

    Some settings are supported by both options and arguments.

    :param param_decls: the parameter declarations for this option or
                        argument.  This is a list of flags or argument
                        names.
    :param type: the type that should be used.  Either a :class:`ParamType`
                 or a Python type.  The latter is converted into the former
                 automatically if supported.
    :param required: controls if this is optional or not.
    :param default: the default value if omitted.  This can also be a callable,
                    in which case it's invoked when the default is needed
                    without any arguments.
    :param callback: A function to further process or validate the value
        after type conversion. It is called as ``f(ctx, param, value)``
        and must return the value. It is called for all sources,
        including prompts.
    :param nargs: the number of arguments to match.  If not ``1`` the return
                  value is a tuple instead of single value.  The default for
                  nargs is ``1`` (except if the type is a tuple, then it's
                  the arity of the tuple). If ``nargs=-1``, all remaining
                  parameters are collected.
    :param metavar: how the value is represented in the help page.
    :param expose_value: if this is `True` then the value is passed onwards
                         to the command callback and stored on the context,
                         otherwise it's skipped.
    :param is_eager: eager values are processed before non eager ones.  This
                     should not be set for arguments or it will inverse the
                     order of processing.
    :param envvar: environment variable(s) that are used to provide a default value for
        this parameter. This can be a string or a sequence of strings. If a sequence is
        given, only the first non-empty environment variable is used for the parameter.
    :param shell_complete: A function that returns custom shell
        completions. Used instead of the param's type completion if
        given. Takes ``ctx, param, incomplete`` and must return a list
        of :class:`~click.shell_completion.CompletionItem` or a list of
        strings.
    :param deprecated: If ``True`` or non-empty string, issues a message
                        indicating that the argument is deprecated and highlights
                        its deprecation in --help. The message can be customized
                        by using a string as the value. A deprecated parameter
                        cannot be required, a ValueError will be raised otherwise.

    .. versionchanged:: 8.2.0
        Introduction of ``deprecated``.

    .. versionchanged:: 8.2
        Adding duplicate parameter names to a :class:`~click.core.Command` will
        result in a ``UserWarning`` being shown.

    .. versionchanged:: 8.2
        Adding duplicate parameter names to a :class:`~click.core.Command` will
        result in a ``UserWarning`` being shown.

    .. versionchanged:: 8.0
        ``process_value`` validates required parameters and bounded
        ``nargs``, and invokes the parameter callback before returning
        the value. This allows the callback to validate prompts.
        ``full_process_value`` is removed.

    .. versionchanged:: 8.0
        ``autocompletion`` is renamed to ``shell_complete`` and has new
        semantics described above. The old name is deprecated and will
        be removed in 8.1, until then it will be wrapped to match the
        new requirements.

    .. versionchanged:: 8.0
        For ``multiple=True, nargs>1``, the default must be a list of
        tuples.

    .. versionchanged:: 8.0
        Setting a default is no longer required for ``nargs>1``, it will
        default to ``None``. ``multiple=True`` or ``nargs=-1`` will
        default to ``()``.

    .. versionchanged:: 7.1
        Empty environment variables are ignored rather than taking the
        empty string value. This makes it possible for scripts to clear
        variables if they can't unset them.

    .. versionchanged:: 2.0
        Changed signature for parameter callback to also be passed the
        parameter. The old callback format will still work, but it will
        raise a warning to give you a chance to migrate the code easier.
    """

    param_type_name = "parameter"

    def __init__(
        self,
        param_decls: cabc.Sequence[str] | None = None,
        type: types.ParamType[t.Any] | t.Any | None = None,
        required: bool = False,
        # XXX The default historically embed two concepts:
        # - the declaration of a Parameter object carrying the default (handy to
        #   arbitrage the default value of coupled Parameters sharing the same
        #   self.name, like flag options),
        # - and the actual value of the default.
        # It is confusing and is the source of many issues discussed in:
        # https://github.com/pallets/click/pull/3030
        # In the future, we might think of splitting it in two, not unlike
        # Option.is_flag and Option.flag_value: we could have something like
        # Parameter.is_default and Parameter.default_value.
        default: t.Any | t.Callable[[], t.Any] | None = UNSET,
        callback: t.Callable[[Context, Parameter, t.Any], t.Any] | None = None,
        nargs: int | None = None,
        multiple: bool = False,
        metavar: str | None = None,
        expose_value: bool = True,
        is_eager: bool = False,
        envvar: str | cabc.Sequence[str] | None = None,
        shell_complete: t.Callable[
            [Context, Parameter, str], list[CompletionItem] | list[str]
        ]
        | None = None,
        deprecated: bool | str = False,
    ) -> None:
        self.name: str
        self.opts: list[str]
        self.secondary_opts: list[str]
        self.name, self.opts, self.secondary_opts = self._parse_decls(
            param_decls or (), expose_value
        )
        self.type: types.ParamType[t.Any] = types.convert_type(type, default)

        # Default nargs to what the type tells us if we have that
        # information available.
        if nargs is None:
            if self.type.is_composite:
                nargs = self.type.arity
            else:
                nargs = 1

        self.required = required
        self.callback = callback
        self.nargs = nargs
        self.multiple = multiple
        self.expose_value = expose_value
        self.default: t.Any | t.Callable[[], t.Any] | None = default
        # Whether the user passed ``default`` explicitly to the constructor.
        # Captured before any auto-derived default (like ``False`` for boolean
        # flags in :class:`Option`) replaces the :data:`UNSET` sentinel, so it
        # remains ``False`` when the default was inferred rather than chosen.
        # Refs: https://github.com/pallets/click/issues/3403
        self._default_explicit: bool = default is not UNSET
        self.is_eager = is_eager
        self.metavar = metavar
        self.envvar = envvar
        self._custom_shell_complete = shell_complete
        self.deprecated = deprecated

        if __debug__:
            if self.type.is_composite and nargs != self.type.arity:
                raise ValueError(
                    f"对于类型 {self.type!r}，'nargs' 必须为 {self.type.arity}(或 None)，但实际为 {nargs}。"
                )

            if required and deprecated:
                raise ValueError(
                    f"{self.param_type_name} '{self.human_readable_name}' 已被弃用，但仍需保留。"
                    f"被弃用的 {self.param_type_name} 不能再被要求使用。"
                )

    def to_info_dict(self) -> dict[str, t.Any]:
        """Gather information that could be useful for a tool generating
        user-facing documentation.

        Use :meth:`click.Context.to_info_dict` to traverse the entire
        CLI structure.

        .. versionchanged:: 8.3.0
            Returns ``None`` for the :attr:`default` if it was not set.

        .. versionadded:: 8.0
        """
        return {
            "name": self.name,
            "param_type_name": self.param_type_name,
            "opts": self.opts,
            "secondary_opts": self.secondary_opts,
            "type": self.type.to_info_dict(),
            "required": self.required,
            "nargs": self.nargs,
            "multiple": self.multiple,
            # We explicitly hide the :attr:`UNSET` value to the user, as we choose to
            # make it an implementation detail. And because ``to_info_dict`` has been
            # designed for documentation purposes, we return ``None`` instead.
            "default": self.default if self.default is not UNSET else None,
            "envvar": self.envvar,
        }

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.name}>"

    @abstractmethod
    def _parse_decls(
        self, decls: cabc.Sequence[str], expose_value: bool
    ) -> tuple[str, list[str], list[str]]: ...

    @property
    def human_readable_name(self) -> str:
        """Returns the human readable name of this parameter.  This is the
        same as the name for options, but the metavar for arguments.
        """
        return self.name

    def make_metavar(self, ctx: Context) -> str:
        if self.metavar is not None:
            return self.metavar

        metavar = self.type.get_metavar(param=self, ctx=ctx)

        if metavar is None:
            metavar = self.type.name.upper()

        if self.nargs != 1:
            metavar += "..."

        return metavar

    @t.overload
    def get_default(
        self, ctx: Context, call: t.Literal[True] = True
    ) -> t.Any | None: ...

    @t.overload
    def get_default(
        self, ctx: Context, call: bool = ...
    ) -> t.Any | t.Callable[[], t.Any] | None: ...

    def get_default(
        self, ctx: Context, call: bool = True
    ) -> t.Any | t.Callable[[], t.Any] | None:
        """Get the default for the parameter. Tries
        :meth:`Context.lookup_default` first, then the local default.

        :param ctx: Current context.
        :param call: If the default is a callable, call it. Disable to
            return the callable instead.

        .. versionchanged:: 8.0.2
            Type casting is no longer performed when getting a default.

        .. versionchanged:: 8.0.1
            Type casting can fail in resilient parsing mode. Invalid
            defaults will not prevent showing help text.

        .. versionchanged:: 8.0
            Looks at ``ctx.default_map`` first.

        .. versionchanged:: 8.0
            Added the ``call`` parameter.
        """
        value = ctx.lookup_default(self.name, call=False)

        if value is None and not ctx._default_map_has(self.name):
            value = self.default

        if call and callable(value):
            value = value()

        return value

    @abstractmethod
    def add_to_parser(self, parser: _OptionParser, ctx: Context) -> None: ...

    def consume_value(
        self, ctx: Context, opts: cabc.Mapping[str, t.Any]
    ) -> tuple[t.Any, ParameterSource]:
        """Returns the parameter value produced by the parser.

        If the parser did not produce a value from user input, the value is either
        sourced from the environment variable, the default map, or the parameter's
        default value. In that order of precedence.

        If no value is found, an internal sentinel value is returned.

        :meta private:
        """
        # Collect from the parse the value passed by the user to the CLI.
        value = opts.get(self.name, UNSET)
        # If the value is set, it means it was sourced from the command line by the
        # parser, otherwise it left unset by default.
        source = (
            ParameterSource.COMMANDLINE
            if value is not UNSET
            else ParameterSource.DEFAULT
        )

        if value is UNSET:
            envvar_value = self.value_from_envvar(ctx)
            if envvar_value is not None:
                value = envvar_value
                source = ParameterSource.ENVIRONMENT

        if value is UNSET:
            default_map_value = ctx.lookup_default(self.name)
            if default_map_value is not None or ctx._default_map_has(self.name):
                value = default_map_value
                source = ParameterSource.DEFAULT_MAP

                # A string from default_map must be split for multi-value
                # parameters, matching value_from_envvar behavior.
                if isinstance(value, str) and self.nargs != 1:
                    value = self.type.split_envvar_value(value)

        if value is UNSET:
            default_value = self.get_default(ctx)
            if default_value is not UNSET:
                value = default_value
                source = ParameterSource.DEFAULT

        return value, source

    def type_cast_value(self, ctx: Context, value: t.Any) -> t.Any:
        """Convert and validate a value against the parameter's
        :attr:`type`, :attr:`multiple`, and :attr:`nargs`.
        """
        if value is None:
            if self.multiple or self.nargs == -1:
                return ()
            else:
                return value

        def check_iter(value: t.Any) -> cabc.Iterator[t.Any]:
            try:
                return _check_iter(value)
            except TypeError:
                # This should only happen when passing in args manually,
                # the parser should construct an iterable when parsing
                # the command line.
                raise BadParameter(
                    _("值必须是一个可迭代对象"), ctx=ctx, param=self
                ) from None

        # Define the conversion function based on nargs and type.

        if self.nargs == 1 or self.type.is_composite:

            def convert(value: t.Any) -> t.Any:
                return self.type(value, param=self, ctx=ctx)

        elif self.nargs == -1:

            def convert(value: t.Any) -> t.Any:  # tuple[t.Any, ...]
                return tuple(self.type(x, self, ctx) for x in check_iter(value))

        else:  # nargs > 1

            def convert(value: t.Any) -> t.Any:  # tuple[t.Any, ...]
                value = tuple(check_iter(value))

                if len(value) != self.nargs:
                    raise BadParameter(
                        ngettext(
                            "需要 {nargs} 个值，但只提供了 1 个",
                            "需要 {nargs} 个值，但提供了 {len} 个",
                            len(value),
                        ).format(nargs=self.nargs, len=len(value)),
                        ctx=ctx,
                        param=self,
                    )

                return tuple(self.type(x, self, ctx) for x in value)

        if self.multiple:
            return tuple(convert(x) for x in check_iter(value))

        return convert(value)

    def value_is_missing(self, value: t.Any) -> bool:
        """A value is considered missing if:

        - it is :attr:`UNSET`,
        - or if it is an empty sequence while the parameter is suppose to have
          non-single value (i.e. :attr:`nargs` is not ``1`` or :attr:`multiple` is
          set).

        :meta private:
        """
        if value is UNSET:
            return True

        if (self.nargs != 1 or self.multiple) and value == ():
            return True

        return False

    def process_value(self, ctx: Context, value: t.Any) -> t.Any:
        """Process the value of this parameter:

        1. Type cast the value using :meth:`type_cast_value`.
        2. Check if the value is missing (see: :meth:`value_is_missing`), and raise
           :exc:`MissingParameter` if it is required.
        3. If a :attr:`callback` is set, call it to have the value replaced by the
           result of the callback. If the value was not set, the callback receive
           ``None``. This keep the legacy behavior as it was before the introduction of
           the :attr:`UNSET` sentinel.

        :meta private:
        """
        # shelter `type_cast_value` from ever seeing an `UNSET` value by handling the
        # cases in which `UNSET` gets special treatment explicitly at this layer
        #
        # Refs:
        # https://github.com/pallets/click/issues/3069
        if value is UNSET:
            if self.multiple or self.nargs == -1:
                value = ()
        else:
            value = self.type_cast_value(ctx, value)

        if self.required and self.value_is_missing(value):
            raise MissingParameter(ctx=ctx, param=self)

        if self.callback is not None:
            # Legacy case: UNSET is not exposed directly to the callback, but converted
            # to None.
            if value is UNSET:
                value = None

            # Search for parameters with UNSET values in the context.
            unset_keys = {k: None for k, v in ctx.params.items() if v is UNSET}
            # No UNSET values, call the callback as usual.
            if not unset_keys:
                value = self.callback(ctx, self, value)

            # Legacy case: provide a temporarily manipulated context to the callback
            # to hide UNSET values as None.
            #
            # Refs:
            # https://github.com/pallets/click/issues/3136
            # https://github.com/pallets/click/pull/3137
            else:
                # Add another layer to the context stack to clearly hint that the
                # context is temporarily modified.
                with ctx:
                    # Update the context parameters to replace UNSET with None.
                    ctx.params.update(unset_keys)
                    # Feed these fake context parameters to the callback.
                    value = self.callback(ctx, self, value)
                    # Restore the UNSET values in the context parameters.
                    ctx.params.update(
                        {
                            k: UNSET
                            for k in unset_keys
                            # Only restore keys that are present and still None, in case
                            # the callback modified other parameters.
                            if k in ctx.params and ctx.params[k] is None
                        }
                    )

        return value

    def resolve_envvar_value(self, ctx: Context) -> str | None:
        """Returns the value found in the environment variable(s) attached to this
        parameter.

        Environment variables values are `always returned as strings
        <https://docs.python.org/3/library/os.html#os.environ>`_.

        This method returns ``None`` if:

        - the :attr:`envvar` property is not set on the :class:`Parameter`,
        - the environment variable is not found in the environment,
        - the variable is found in the environment but its value is empty (i.e. the
          environment variable is present but has an empty string).

        If :attr:`envvar` is setup with multiple environment variables,
        then only the first non-empty value is returned.

        .. caution::

            The raw value extracted from the environment is not normalized and is
            returned as-is. Any normalization or reconciliation is performed later by
            the :class:`Parameter`'s :attr:`type`.

        :meta private:
        """
        if not self.envvar:
            return None

        if isinstance(self.envvar, str):
            rv = os.environ.get(self.envvar)

            if rv:
                return rv
        else:
            for envvar in self.envvar:
                rv = os.environ.get(envvar)

                # Return the first non-empty value of the list of environment variables.
                if rv:
                    return rv
                # Else, absence of value is interpreted as an environment variable that
                # is not set, so proceed to the next one.

        return None

    def value_from_envvar(self, ctx: Context) -> str | cabc.Sequence[str] | None:
        """Process the raw environment variable string for this parameter.

        Returns the string as-is or splits it into a sequence of strings if the
        parameter is expecting multiple values (i.e. its :attr:`nargs` property is set
        to a value other than ``1``).

        :meta private:
        """
        rv = self.resolve_envvar_value(ctx)

        if rv is not None and self.nargs != 1:
            return self.type.split_envvar_value(rv)

        return rv

    def handle_parse_result(
        self, ctx: Context, opts: cabc.Mapping[str, t.Any], args: list[str]
    ) -> tuple[t.Any, list[str]]:
        """Process the value produced by the parser from user input.

        Always process the value through the Parameter's :attr:`type`, wherever it
        comes from.

        If the parameter is deprecated, this method warn the user about it. But only if
        the value has been explicitly set by the user (and as such, is not coming from
        a default).

        :meta private:
        """
        # Capture the slot's existing state before we mutate
        # ``_parameter_source`` so the write decision below can compare our
        # incoming source against the source of the option that already wrote
        # the slot (if any).
        existing_value = ctx.params.get(self.name, UNSET)
        existing_source = ctx.get_parameter_source(self.name)
        existing_default_explicit = ctx._param_default_explicit.get(self.name, False)

        with augment_usage_errors(ctx, param=self):
            value, source = self.consume_value(ctx, opts)

            # Record the source before processing so eager callbacks and type
            # conversion can inspect it. Restored after arbitration if this
            # option loses a feature-switch group.
            ctx.set_parameter_source(self.name, source)

            # Display a deprecation warning if necessary.
            if (
                self.deprecated
                and value is not UNSET
                and source < ParameterSource.DEFAULT_MAP
            ):
                message = _(
                    "弃用警告: {param_type} {name!r} 已被弃用。{extra_message}"
                ).format(
                    param_type=self.param_type_name,
                    name=self.human_readable_name,
                    extra_message=_format_deprecated_suffix(self.deprecated),
                )
                echo(style(message, fg="red"), err=True)

            # Process the value through the parameter's type.
            try:
                value = self.process_value(ctx, value)
            except Exception:
                if not ctx.resilient_parsing:
                    raise
                # In resilient parsing mode, we do not want to fail the command if the
                # value is incompatible with the parameter type, so we reset the value
                # to UNSET, which will be interpreted as a missing value.
                value = UNSET

        # Arbitrate the slot when several parameters target the same variable
        # name (feature-switch groups). See: https://github.com/pallets/click/issues/3403
        slot_empty = existing_value is UNSET
        more_explicit = existing_source is not None and source < existing_source
        same_source = existing_source is not None and source == existing_source
        auto_would_downgrade_explicit = (
            same_source
            and source == ParameterSource.DEFAULT
            and existing_default_explicit
            and not self._default_explicit
        )
        is_winner = (
            slot_empty
            or more_explicit
            or (same_source and not auto_would_downgrade_explicit)
        )

        if is_winner:
            if self.expose_value:
                ctx.params[self.name] = value
                ctx._param_default_explicit[self.name] = self._default_explicit
        elif existing_source is not None:
            # Lost arbitration; restore the winning option's source.
            ctx.set_parameter_source(self.name, existing_source)
        # else: ctx.params[self.name] was populated by code that bypassed
        # handle_parse_result (from another option's callback for example). Keep
        # the provisional source recorded before process_value so downstream
        # lookups don't return ``None``.

        return value, args

    def get_help_record(self, ctx: Context) -> tuple[str, str] | None:
        return None

    def get_usage_pieces(self, ctx: Context) -> list[str]:
        return []

    def get_error_hint(self, ctx: Context | None) -> str:
        """Get a stringified version of the param for use in error messages to
        indicate which param caused the error.

        .. versionchanged:: 8.4.0
            ``ctx`` can be ``None``.
        """
        hint_list = self.opts or [self.human_readable_name]
        return " / ".join(f"'{x}'" for x in hint_list)

    def shell_complete(self, ctx: Context, incomplete: str) -> list[CompletionItem]:
        """Return a list of completions for the incomplete value. If a
        ``shell_complete`` function was given during init, it is used.
        Otherwise, the :attr:`type`
        :meth:`~click.types.ParamType[t.Any].shell_complete` function is used.

        :param ctx: Invocation context for this command.
        :param incomplete: Value being completed. May be empty.

        .. versionadded:: 8.0
        """
        if self._custom_shell_complete is not None:
            results = self._custom_shell_complete(ctx, self, incomplete)

            if results and isinstance(results[0], str):
                from click.shell_completion import CompletionItem

                results = [CompletionItem(c) for c in results]

            return t.cast("list[CompletionItem]", results)

        return self.type.shell_complete(ctx, self, incomplete)


class Option(Parameter):
    """
    选项通常是命令行中的可选值，并且具有一些参数所不具备的额外特性。

    所有其他参数都会传递给参数构造函数。

    :param show_default: 在帮助文本中显示该选项的默认值。默认情况下不会显示值，除非 :attr:`Context.show_default` 为 ``True``。如果该值是字符串，它将显示该字符串（带括号），而不是实际值。这对于动态选项特别有用。对于单个选项的布尔标志，如果其值为 ``False``，则默认值仍会隐藏
    :param show_envvar: 控制是否在帮助页面和错误消息中显示环境变量。通常情况下，环境变量不会显示。 :param prompt: 如果设置为 ``True`` 或非空字符串，则会提示用户输入。如果设置为 ``True``，则提示信息将为该选项名称的大写形式。已弃用的选项不能被提示
    :param confirmation_prompt: 如果该选项曾被提示输入，则再次提示以确认该值。可以将其设置为字符串而非 ``True``，以自定义提示信息
    :param prompt_required: 如果设置为 ``False``，则仅当该选项被指定为不带值的标志时，才会提示用户输入
    :param hide_input: 如果设置为 ``True``，则提示输入的内容将对用户隐藏。这对于密码输入非常有用
    :param is_flag: 强制将该选项作为标志处理。默认值为自动检测
    :param flag_value: 如果该标志被启用，则应使用哪个值。如果选项字符串中包含斜杠以标记两个选项，则该值会自动设置为布尔值
    :param multiple: 如果设置为 `True`，则该参数可以接受多次输入并记录。其工作方式与 ``nargs`` 类似，但支持任意数量的参数
    :param count: 该标志使选项能够递增一个整数值
    :param allow_from_autoenv: 如果启用此选项，则当上下文中定义了前缀时，该参数的值将从环境变量中获取
    :param help: 帮助字符串
    :param hidden: 将该选项从帮助输出中隐藏
    :param attrs: 其他命令参数，详见 :class:`Parameter`

    .. versionchanged:: 8.4.0
        非基本类型的 ``flag_value``（不是 ``str``、``int``、``float`` 或 ``bool``）将保持原样传递，而不会被转换为字符串。以前，需要使用 ``type=click.UNPROCESSED`` 才能保留这些类型。

    .. versionchanged:: 8.2
        与 ``flag_value`` 一起使用的 ``envvar`` 将始终使用 ``flag_value``，而以前它会使用环境变量的值。

    .. versionchanged:: 8.1
        帮助文本的缩进在此处进行了清理，而不仅仅是在 ``@option`` 装饰器中。

    .. versionchanged:: 8.1
        ``show_default`` 参数会覆盖 ``Context.show_default`` 的设置。

    .. versionchanged:: 8.1
        如果默认值为 ``False``，则不会显示单个选项布尔标志的默认值。

    .. versionchanged:: 8.0.1
        如果提供了 ``flag_value``，则会从 ``flag_value`` 中检测 ``type``，适用于基本的 Python 类型（``str``、``int``、``float``、``bool``）。
    """

    param_type_name = "option"

    def __init__(
        self,
        param_decls: cabc.Sequence[str] | None = None,
        show_default: bool | str | None = None,
        prompt: bool | str = False,
        confirmation_prompt: bool | str = False,
        prompt_required: bool = True,
        hide_input: bool = False,
        is_flag: bool | None = None,
        flag_value: t.Any = UNSET,
        multiple: bool = False,
        count: bool = False,
        allow_from_autoenv: bool = True,
        type: types.ParamType[t.Any] | t.Any | None = None,
        help: str | None = None,
        hidden: bool = False,
        show_choices: bool = True,
        show_envvar: bool = False,
        deprecated: bool | str = False,
        **attrs: t.Any,
    ) -> None:
        if help:
            help = inspect.cleandoc(help)

        super().__init__(
            param_decls, type=type, multiple=multiple, deprecated=deprecated, **attrs
        )

        if prompt is True:
            if not self.name:
                raise TypeError("'name' 在 'prompt=True' 的情况下是必需的")

            prompt_text: str | None = self.name.replace("_", " ").capitalize()
        elif prompt is False:
            prompt_text = None
        else:
            prompt_text = prompt

        if deprecated:
            label = _format_deprecated_label(deprecated)
            help = f"{help} {label}" if help is not None else label

        self.prompt = prompt_text
        self.confirmation_prompt = confirmation_prompt
        self.prompt_required = prompt_required
        self.hide_input = hide_input
        self.hidden = hidden

        # The _flag_needs_value property tells the parser that this option is a flag
        # that cannot be used standalone and needs a value. With this information, the
        # parser can determine whether to consider the next user-provided argument in
        # the CLI as a value for this flag or as a new option.
        # If prompt is enabled but not required, then it opens the possibility for the
        # option to gets its value from the user.
        self._flag_needs_value = self.prompt is not None and not self.prompt_required

        # Auto-detect if this is a flag or not.
        if is_flag is None:
            # Implicitly a flag because flag_value was set.
            if flag_value is not UNSET:
                is_flag = True
            # Not a flag, but when used as a flag it shows a prompt.
            elif self._flag_needs_value:
                is_flag = False
            # Implicitly a flag because secondary options names were given.
            elif self.secondary_opts:
                is_flag = True

        # The option is explicitly not a flag, but to determine whether or not it needs
        # value, we need to check if `flag_value` or `default` was set. Either one is
        # sufficient.
        # Ref: https://github.com/pallets/click/issues/3084
        elif is_flag is False and not self._flag_needs_value:
            self._flag_needs_value = flag_value is not UNSET or self.default is UNSET

        if is_flag:
            # Set missing default for flags if not explicitly required or prompted.
            if self.default is UNSET and not self.required and not self.prompt:
                if multiple:
                    self.default = ()

            # Auto-detect the type of the flag based on the flag_value.
            if type is None:
                # A flag without a flag_value is a boolean flag.
                if flag_value is UNSET:
                    self.type: types.ParamType[t.Any] = types.BoolParamType()
                # If the flag value is a boolean, use BoolParamType.
                elif isinstance(flag_value, bool):
                    self.type = types.BoolParamType()
                # Otherwise, guess the type from the flag value.
                else:
                    guessed = types.convert_type(None, flag_value)
                    if (
                        isinstance(guessed, types.StringParamType)
                        and not isinstance(flag_value, str)
                        and flag_value is not None
                    ):
                        # The flag_value type couldn't be auto-detected
                        # (not str, int, float, or bool). Since flag_value
                        # is a programmer-provided Python object, not CLI
                        # input, pass it through unchanged instead of
                        # stringifying it.
                        self.type = types.UNPROCESSED
                    else:
                        self.type = guessed

        self.is_flag: bool = bool(is_flag)
        self.is_bool_flag: bool = bool(
            is_flag and isinstance(self.type, types.BoolParamType)
        )
        self.flag_value: t.Any = flag_value

        # Set boolean flag default to False if unset and not required.
        if self.is_bool_flag:
            if self.default is UNSET and not self.required:
                self.default = False

        # The alignment of default to the flag_value is resolved lazily in
        # get_default() to prevent callable flag_values (like classes) from
        # being instantiated. Refs:
        # https://github.com/pallets/click/issues/3121
        # https://github.com/pallets/click/issues/3024#issuecomment-3146199461
        # https://github.com/pallets/click/pull/3030/commits/06847da

        # Set the default flag_value if it is not set.
        if self.flag_value is UNSET:
            if self.is_flag:
                self.flag_value = True
            else:
                self.flag_value = None

        # Counting.
        self.count = count
        if count:
            if type is None:
                self.type = types.IntRange(min=0)
            if self.default is UNSET:
                self.default = 0

        self.allow_from_autoenv = allow_from_autoenv
        self.help = help
        self.show_default = show_default
        self.show_choices = show_choices
        self.show_envvar = show_envvar

        if __debug__:
            if deprecated and prompt:
                raise ValueError("`deprecated` 选项不能使用 `prompt`")

            if self.nargs == -1:
                raise TypeError("不支持为选项设置 nargs=-1")

            if not self.is_bool_flag and self.secondary_opts:
                raise TypeError("次级标志对非布尔标志无效")

            if self.is_bool_flag and self.hide_input and self.prompt is not None:
                raise TypeError(
                    "带有 'hide_input' 的 'prompt' 对于布尔标志无效"
                )

            if self.count:
                if self.multiple:
                    raise TypeError("'count' 不能与 'multiple' 一起使用")

                if self.is_flag:
                    raise TypeError("'count' 不能与 'is_flag' 一起使用")

    def to_info_dict(self) -> dict[str, t.Any]:
        """
        .. versionchanged:: 8.3.0
            Returns ``None`` for the :attr:`flag_value` if it was not set.
        """
        info_dict = super().to_info_dict()
        info_dict.update(
            help=self.help,
            prompt=self.prompt,
            is_flag=self.is_flag,
            # We explicitly hide the :attr:`UNSET` value to the user, as we choose to
            # make it an implementation detail. And because ``to_info_dict`` has been
            # designed for documentation purposes, we return ``None`` instead.
            flag_value=self.flag_value if self.flag_value is not UNSET else None,
            count=self.count,
            hidden=self.hidden,
        )
        return info_dict

    def get_default(
        self, ctx: Context, call: bool = True
    ) -> t.Any | t.Callable[[], t.Any] | None:
        """Return the default value for this option.

        For non-boolean flag options, ``default=True`` is treated as a sentinel meaning "activate this flag by default" and is resolved to :attr:`flag_value`.  For example, with ``--upper/--lower`` feature switches where ``flag_value="upper"`` and ``default=True``, the default resolves to ``"upper"``.

        .. caution::
            This substitution only applies to non-boolean flags (:attr:`is_bool_flag` is ``False``). For boolean flags, ``True`` is a legitimate Python value and ``default=True`` is returned as-is.

        .. versionchanged:: 8.3.3
            ``default=True`` is no longer substituted with ``flag_value`` for boolean flags, fixing negative boolean flags like ``flag_value=False, default=True``.
        """
        value = super().get_default(ctx, call=False)

        # Resolve default=True to flag_value lazily (here instead of
        # __init__) to prevent callable flag_values (like classes) from
        # being instantiated by the callable check below.
        if value is True and self.is_flag and not self.is_bool_flag:
            value = self.flag_value
        elif call and callable(value):
            value = value()

        return value

    def get_error_hint(self, ctx: Context | None) -> str:
        result = super().get_error_hint(ctx)
        if self.show_envvar and self.envvar is not None:
            result += f" (env var: '{self.envvar}')"
        return result

    def _parse_decls(
        self, decls: cabc.Sequence[str], expose_value: bool
    ) -> tuple[str, list[str], list[str]]:
        opts = []
        secondary_opts = []
        name = None
        possible_names = []

        for decl in decls:
            if decl.isidentifier():
                if name is not None:
                    raise TypeError(_("名称 '{name}' 定义了两次").format(name=name))
                name = decl
            else:
                split_char = ";" if decl[:1] == "/" else "/"
                if split_char in decl:
                    first, second = decl.split(split_char, 1)
                    first = first.rstrip()
                    if first:
                        possible_names.append(_split_opt(first))
                        opts.append(first)
                    second = second.lstrip()
                    if second:
                        secondary_opts.append(second.lstrip())
                    if first == second:
                        raise ValueError(
                            _(
                                "布尔选项 {decl!r} 不能使用相同的标志来表示真/假"
                            ).format(decl=decl)
                        )
                else:
                    possible_names.append(_split_opt(decl))
                    opts.append(decl)

        if name is None and possible_names:
            possible_names.sort(key=lambda x: -len(x[0]))  # group long options first
            name = possible_names[0][1].replace("-", "_").lower()
            if not name.isidentifier():
                name = None

        if name is None:
            if not expose_value:
                return "", opts, secondary_opts
            raise TypeError(
                _(
                    "无法为声明为 {decls!r} 的选项确定名称"
                ).format(decls=decls)
            )

        if not opts and not secondary_opts:
            raise TypeError(
                _(
                    "未定义选项，但传入了名称 ({name})。"
                    "您是否本意是声明一个参数？您是否本意是传入 '--{name}'？"
                ).format(name=name)
            )

        return name, opts, secondary_opts

    def add_to_parser(self, parser: _OptionParser, ctx: Context) -> None:
        if self.multiple:
            action = "append"
        elif self.count:
            action = "count"
        else:
            action = "store"

        if self.is_flag:
            action = f"{action}_const"

            if self.is_bool_flag and self.secondary_opts:
                parser.add_option(
                    obj=self, opts=self.opts, dest=self.name, action=action, const=True
                )
                parser.add_option(
                    obj=self,
                    opts=self.secondary_opts,
                    dest=self.name,
                    action=action,
                    const=False,
                )
            else:
                parser.add_option(
                    obj=self,
                    opts=self.opts,
                    dest=self.name,
                    action=action,
                    const=self.flag_value,
                )
        else:
            parser.add_option(
                obj=self,
                opts=self.opts,
                dest=self.name,
                action=action,
                nargs=self.nargs,
            )

    def get_help_record(self, ctx: Context) -> tuple[str, str] | None:
        if self.hidden:
            return None

        any_prefix_is_slash = False

        def _write_opts(opts: cabc.Sequence[str]) -> str:
            nonlocal any_prefix_is_slash

            rv, any_slashes = join_options(opts)

            if any_slashes:
                any_prefix_is_slash = True

            if not self.is_flag and not self.count:
                rv += f" {self.make_metavar(ctx=ctx)}"

            return rv

        rv = [_write_opts(self.opts)]

        if self.secondary_opts:
            rv.append(_write_opts(self.secondary_opts))

        help = self.help or ""

        extra = self.get_help_extra(ctx)
        extra_items = []
        if "envvars" in extra:
            extra_items.append(
                _("env var: {var}").format(var=", ".join(extra["envvars"]))
            )
        if "default" in extra:
            extra_items.append(_("default: {default}").format(default=extra["default"]))
        if "range" in extra:
            extra_items.append(extra["range"])
        if "required" in extra:
            extra_items.append(_(extra["required"]))

        if extra_items:
            extra_str = "; ".join(extra_items)
            help = f"{help}  [{extra_str}]" if help else f"[{extra_str}]"

        return ("; " if any_prefix_is_slash else " / ").join(rv), help

    def get_help_extra(self, ctx: Context) -> types.OptionHelpExtra:
        extra: types.OptionHelpExtra = {}

        if self.show_envvar:
            envvar = self.envvar

            if envvar is None:
                if (
                    self.allow_from_autoenv
                    and ctx.auto_envvar_prefix is not None
                    and self.name
                ):
                    envvar = f"{ctx.auto_envvar_prefix}_{self.name.upper()}"

            if envvar is not None:
                if isinstance(envvar, str):
                    extra["envvars"] = (envvar,)
                else:
                    extra["envvars"] = tuple(str(d) for d in envvar)

        # Temporarily enable resilient parsing to avoid type casting
        # failing for the default. Might be possible to extend this to
        # help formatting in general.
        resilient = ctx.resilient_parsing
        ctx.resilient_parsing = True

        try:
            default_value = self.get_default(ctx, call=False)
        finally:
            ctx.resilient_parsing = resilient

        show_default = False
        show_default_is_str = False

        if self.show_default is not None:
            if isinstance(self.show_default, str):
                show_default_is_str = show_default = True
            else:
                show_default = self.show_default
        elif ctx.show_default is not None:
            show_default = ctx.show_default

        if show_default_is_str or (
            show_default and (default_value not in (None, UNSET))
        ):
            if show_default_is_str:
                default_string = f"({self.show_default})"
            elif isinstance(default_value, (list, tuple)):
                default_string = ", ".join(str(d) for d in default_value)
            elif isinstance(default_value, enum.Enum):
                default_string = default_value.name
            elif inspect.isfunction(default_value):
                default_string = _("(dynamic)")
            elif self.is_bool_flag and self.secondary_opts:
                # For boolean flags that have distinct True/False opts,
                # use the opt without prefix instead of the value.
                default_string = _split_opt(
                    (self.opts if default_value else self.secondary_opts)[0]
                )[1]
            elif self.is_bool_flag and not self.secondary_opts and not default_value:
                default_string = ""
            elif isinstance(default_value, str) and default_value == "":
                default_string = '""'
            else:
                default_string = str(default_value)

            if default_string:
                extra["default"] = default_string

        if (
            isinstance(self.type, types._NumberRangeBase)
            # skip count with default range type
            and not (self.count and self.type.min == 0 and self.type.max is None)
        ):
            range_str = self.type._describe_range()

            if range_str:
                extra["range"] = range_str

        if self.required:
            extra["required"] = "required"

        return extra

    def prompt_for_value(self, ctx: Context) -> t.Any:
        """This is an alternative flow that can be activated in the full
        value processing if a value does not exist.  It will prompt the
        user until a valid value exists and then returns the processed
        value as result.
        """
        assert self.prompt is not None

        # Calculate the default before prompting anything to lock in the value before
        # attempting any user interaction.
        default = self.get_default(ctx)

        # A boolean flag can use a simplified [y/n] confirmation prompt.
        if self.is_bool_flag:
            # If we have no boolean default, we force the user to explicitly provide
            # one.
            if default in (UNSET, None):
                default = None
            # Nothing prevent you to declare an option that is simultaneously:
            # 1) auto-detected as a boolean flag,
            # 2) allowed to prompt, and
            # 3) still declare a non-boolean default.
            # This forced casting into a boolean is necessary to align any non-boolean
            # default to the prompt, which is going to be a [y/n]-style confirmation
            # because the option is still a boolean flag. That way, instead of [y/n],
            # we get [Y/n] or [y/N] depending on the truthy value of the default.
            # Refs: https://github.com/pallets/click/pull/3030#discussion_r2289180249
            else:
                default = bool(default)
            return confirm(self.prompt, default)

        # If show_default is given, provide this to `prompt` as well,
        # otherwise we use `prompt`'s default behavior
        prompt_kwargs: t.Any = {}
        if self.show_default is not None:
            prompt_kwargs["show_default"] = self.show_default

        return prompt(
            self.prompt,
            # Use ``None`` to inform the prompt() function to reiterate until a valid
            # value is provided by the user if we have no default.
            default=None if default is UNSET else default,
            type=self.type,
            hide_input=self.hide_input,
            show_choices=self.show_choices,
            confirmation_prompt=self.confirmation_prompt,
            value_proc=lambda x: self.process_value(ctx, x),
            **prompt_kwargs,
        )

    def resolve_envvar_value(self, ctx: Context) -> str | None:
        """:class:`Option` resolves its environment variable the same way as
        :func:`Parameter.resolve_envvar_value`, but it also supports
        :attr:`Context.auto_envvar_prefix`. If we could not find an environment from
        the :attr:`envvar` property, we fallback on :attr:`Context.auto_envvar_prefix`
        to build dynamiccaly the environment variable name using the
        :python:`{ctx.auto_envvar_prefix}_{self.name.upper()}` template.

        :meta private:
        """
        rv = super().resolve_envvar_value(ctx)

        if rv is not None:
            return rv

        if self.allow_from_autoenv and ctx.auto_envvar_prefix is not None and self.name:
            envvar = f"{ctx.auto_envvar_prefix}_{self.name.upper()}"
            rv = os.environ.get(envvar)

            if rv:
                return rv

        return None

    def value_from_envvar(self, ctx: Context) -> t.Any:
        """For :class:`Option`, this method processes the raw environment variable
        string the same way as :func:`Parameter.value_from_envvar` does.

        But in the case of non-boolean flags, the value is analyzed to determine if the
        flag is activated or not, and returns a boolean of its activation, or the
        :attr:`flag_value` if the latter is set.

        This method also takes care of repeated options (i.e. options with
        :attr:`multiple` set to ``True``).

        :meta private:
        """
        rv = self.resolve_envvar_value(ctx)

        # Absent environment variable or an empty string is interpreted as unset.
        if rv is None:
            return None

        # Non-boolean flags are more liberal in what they accept. But a flag being a
        # flag, its envvar value still needs to be analyzed to determine if the flag is
        # activated or not.
        if self.is_flag and not self.is_bool_flag:
            # If the flag_value is set and match the envvar value, return it
            # directly.
            if self.flag_value is not UNSET and rv == self.flag_value:
                return self.flag_value
            # Analyze the envvar value as a boolean to know if the flag is
            # activated or not.
            return types.BoolParamType.str_to_bool(rv)

        # Split the envvar value if it is allowed to be repeated.
        value_depth = (self.nargs != 1) + bool(self.multiple)
        if value_depth > 0:
            multi_rv = self.type.split_envvar_value(rv)
            if self.multiple and self.nargs != 1:
                multi_rv = batch(multi_rv, self.nargs)  # type: ignore[assignment]

            return multi_rv

        return rv

    def consume_value(
        self, ctx: Context, opts: cabc.Mapping[str, Parameter]
    ) -> tuple[t.Any, ParameterSource]:
        """For :class:`Option`, the value can be collected from an interactive prompt
        if the option is a flag that needs a value (and the :attr:`prompt` property is
        set).

        Additionally, this method handles flag option that are activated without a
        value, in which case the :attr:`flag_value` is returned.

        :meta private:
        """
        value, source = super().consume_value(ctx, opts)

        # The parser will emit a sentinel value if the option is allowed to as a flag
        # without a value.
        if value is FLAG_NEEDS_VALUE:
            # If the option allows for a prompt, we start an interaction with the user.
            if self.prompt is not None and not ctx.resilient_parsing:
                value = self.prompt_for_value(ctx)
                source = ParameterSource.PROMPT
            # Else the flag takes its flag_value as value.
            else:
                value = self.flag_value
                source = ParameterSource.COMMANDLINE

        # A flag which is activated always returns the flag value, unless the value
        # comes from the explicitly sets default.
        elif (
            self.is_flag
            and value is True
            and not self.is_bool_flag
            and source < ParameterSource.DEFAULT_MAP
        ):
            value = self.flag_value

        # Re-interpret a multiple option which has been sent as-is by the parser.
        # Here we replace each occurrence of value-less flags (marked by the
        # FLAG_NEEDS_VALUE sentinel) with the flag_value.
        elif (
            self.multiple
            and value is not UNSET
            and isinstance(value, cabc.Iterable)
            and source < ParameterSource.DEFAULT_MAP
            and any(v is FLAG_NEEDS_VALUE for v in value)
        ):
            value = [self.flag_value if v is FLAG_NEEDS_VALUE else v for v in value]
            source = ParameterSource.COMMANDLINE

        # The value wasn't set, or used the param's default, prompt for one to the user
        # if prompting is enabled.
        elif (
            (value is UNSET or source >= ParameterSource.DEFAULT_MAP)
            and self.prompt is not None
            and (self.required or self.prompt_required)
            and not ctx.resilient_parsing
        ):
            value = self.prompt_for_value(ctx)
            source = ParameterSource.PROMPT

        return value, source

    def process_value(self, ctx: Context, value: t.Any) -> t.Any:
        # process_value has to be overridden on Options in order to capture
        # `value == UNSET` cases before `type_cast_value()` gets called.
        #
        # Refs:
        # https://github.com/pallets/click/issues/3069
        if self.is_flag and not self.required and self.is_bool_flag and value is UNSET:
            value = False

            if self.callback is not None:
                value = self.callback(ctx, self, value)

            return value

        # in the normal case, rely on Parameter.process_value
        return super().process_value(ctx, value)


class Argument(Parameter):
    """Arguments are positional parameters to a command.  They generally
    provide fewer features than options but can have infinite ``nargs``
    and are required by default.

    All parameters are passed onwards to the constructor of :class:`Parameter`.
    """

    param_type_name = "argument"

    def __init__(
        self,
        param_decls: cabc.Sequence[str],
        required: bool | None = None,
        **attrs: t.Any,
    ) -> None:
        # Auto-detect the requirement status of the argument if not explicitly set.
        if required is None:
            # The argument gets automatically required if it has no explicit default
            # value set and is setup to match at least one value.
            if attrs.get("default", UNSET) is UNSET:
                required = attrs.get("nargs", 1) > 0
            # If the argument has a default value, it is not required.
            else:
                required = False

        if "multiple" in attrs:
            raise TypeError("__init__() 接收到了一个意外的关键字参数 'multiple'")

        super().__init__(param_decls, required=required, **attrs)

    @property
    def human_readable_name(self) -> str:
        if self.metavar is not None:
            return self.metavar
        return self.name.upper()

    def make_metavar(self, ctx: Context) -> str:
        if self.metavar is not None:
            return self.metavar
        var = self.type.get_metavar(param=self, ctx=ctx)
        if not var:
            var = self.name.upper()
        if self.deprecated:
            var += "!"
        if not self.required:
            var = f"[{var}]"
        if self.nargs != 1:
            var += "..."
        return var

    def _parse_decls(
        self, decls: cabc.Sequence[str], expose_value: bool
    ) -> tuple[str, list[str], list[str]]:
        if not decls:
            if not expose_value:
                return "", [], []
            raise TypeError("参数被标记为已暴露，但没有名称")
        if len(decls) == 1:
            name = arg = decls[0]
            name = name.replace("-", "_").lower()
        else:
            raise TypeError(
                _(
                    "参数声明必须恰好包含一个参数，但实际获取了 {length}: {decls}"
                ).format(length=len(decls), decls=decls)
            )
        return name, [arg], []

    def get_usage_pieces(self, ctx: Context) -> list[str]:
        return [self.make_metavar(ctx)]

    def get_error_hint(self, ctx: Context | None) -> str:
        if ctx is not None:
            return f"'{self.make_metavar(ctx)}'"
        return f"'{self.human_readable_name}'"

    def add_to_parser(self, parser: _OptionParser, ctx: Context) -> None:
        parser.add_argument(dest=self.name, nargs=self.nargs, obj=self)


def __getattr__(name: str) -> object:
    import warnings

    if name == "BaseCommand":
        warnings.warn(
            "'BaseCommand' 已弃用，并将在 Click 9.0 中移除。请改用 'Command'",
            DeprecationWarning,
            stacklevel=2,
        )
        return _BaseCommand

    if name == "MultiCommand":
        warnings.warn(
            "'MultiCommand' 已弃用，并将在 Click 9.0 中移除。请改用 'Group'",
            DeprecationWarning,
            stacklevel=2,
        )
        return _MultiCommand

    raise AttributeError(name)

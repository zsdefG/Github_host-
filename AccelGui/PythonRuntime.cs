using System.IO;
using Microsoft.Win32;
using Python.Runtime;

namespace AccelGui;

/// <summary>
/// pythonnet 引擎单例：定位 python DLL → 初始化引擎 → 加载 accel_server 模块。
/// 对外暴露 dynamic Module，调用 Python 函数由 DLR 自动转换参数并管理 GIL（pythonnet 官方模式）。
/// </summary>
public static class PythonRuntime
{
    /// <summary>UI 日志出口（MainWindow 注册；回调里禁止再进入 Python）。</summary>
    public static Action<string, string>? LogSink { get; set; }

    private static readonly object Gate = new();
    private static bool _initialized;
    private static IntPtr _threadState;
    private static PyObject? _module;
    private static bool _callbackWired;

    public static bool IsReady
    {
        get { lock (Gate) return _initialized; }
    }

    /// <summary>
    /// 随应用发布的嵌入式 Python 运行时目录：{BaseDir}\runtime\python\（含 python3xx.dll、Lib\、DLLs\）。
    /// 打包脚本 pack-runtime.ps1 在构建时从本机 Python 复制而来，使目标机器无需安装 Python。
    /// </summary>
    private static (string Dll, string Home)? TryLocateBundledRuntime()
    {
        var root = Path.Combine(AppContext.BaseDirectory, "runtime", "python");
        if (!Directory.Exists(root)) return null;

        // python3.dll 是稳定 ABI 桥，不是解释器本体；只认 python3xx.dll（如 python312.dll）
        string? dll = null;
        try
        {
            foreach (var f in Directory.GetFiles(root, "python3*.dll"))
            {
                if (System.Text.RegularExpressions.Regex.IsMatch(Path.GetFileName(f), @"^python3\d+\.dll$"))
                { dll = f; break; }
            }
        }
        catch (Exception) { return null; }

        if (dll == null || !Directory.Exists(Path.Combine(root, "Lib"))) return null;
        return (dll, root);
    }

    /// <summary>尝试定位 Python DLL（pythonnet 的 Runtime.PythonDLL 必须是完整路径）。</summary>
    /// <param name="manualOverride">用户在设置里手动指定的完整路径；为空则自动探测。</param>
    public static string? LocatePythonDll(string? manualOverride = null)
    {
        if (!string.IsNullOrWhiteSpace(manualOverride) && File.Exists(manualOverride))
            return manualOverride;

        // 1) 环境变量显式指定
        var env = Environment.GetEnvironmentVariable("PYTHONNET_PYDLL");
        if (!string.IsNullOrEmpty(env) && File.Exists(env))
            return env;

        // 2) 注册表 InstallPath（pythonnet 3.x 支持 3.10-3.14）
        foreach (var ver in new[] { "3.12", "3.11", "3.13", "3.14", "3.10" })
        {
            foreach (var hive in new[] { Registry.CurrentUser, Registry.LocalMachine })
            {
                try
                {
                    using var k = hive.OpenSubKey($@"SOFTWARE\Python\PythonCore\{ver}\InstallPath");
                    if (k?.GetValue("") is string dir && !string.IsNullOrEmpty(dir))
                    {
                        var dll = Path.Combine(dir, $"python{ver.Replace(".", "")}.dll");
                        if (File.Exists(dll))
                            return dll;
                    }
                }
                catch (Exception)
                {
                    // 该注册表项不存在或无权限，继续下一个
                }
            }
        }
        return null;
    }

    /// <summary>
    /// 初始化引擎（幂等）。调用方在 UI 线程做一次；之后的 Python 调用由 DLR 在各线程自动进出 GIL。
    /// </summary>
    /// <param name="pythonDll">手动指定 python DLL 完整路径；null 则自动探测。</param>
    /// <exception cref="InvalidOperationException">找不到 DLL / 初始化失败时抛出（带可读原因）。</exception>
    public static void EnsureInitialized(string? pythonDll = null)
    {
        lock (Gate)
        {
            if (_initialized) return;

            // 优先使用随应用发布的嵌入式运行时（目标机器无需安装 Python）
            if (string.IsNullOrWhiteSpace(pythonDll))
            {
                var bundled = TryLocateBundledRuntime();
                if (bundled != null)
                {
                    Runtime.PythonDLL = bundled.Value.Dll;
                    // 嵌入式运行时的标准库 Lib\ 与扩展 DLLs\ 都在 Home 下，必须显式指定 PYTHONHOME，
                    // 否则 CPython 会去注册表/默认路径找标准库而失败。
                    Environment.SetEnvironmentVariable("PYTHONHOME", bundled.Value.Home);
                    PythonEngine.Initialize();
                    _threadState = PythonEngine.BeginAllowThreads();
                    _initialized = true;
                    return;
                }
            }

            var dll = LocatePythonDll(pythonDll)
                ?? throw new InvalidOperationException(
                    "未找到 Python 3.10-3.14 运行库（python*.dll）。请在“设置”里手动指定 Python DLL 路径。");

            Runtime.PythonDLL = dll;
            PythonEngine.Initialize();
            _threadState = PythonEngine.BeginAllowThreads();
            _initialized = true;
        }
    }

    /// <summary>
    /// 惰性获取 accel_server 模块（首次调用会注入脚本目录、挂日志回调）。
    /// </summary>
    /// <param name="scriptDir">accel_server.py 所在目录（sys.path 注入）。</param>
    public static PyObject GetModule(string scriptDir)
    {
        lock (Gate)
        {
            if (_module != null) return _module;

            if (!_initialized)
                throw new InvalidOperationException("引擎未初始化，请先调用 EnsureInitialized。");
            if (!Directory.Exists(scriptDir))
                throw new InvalidOperationException($"脚本目录不存在: {scriptDir}");

            using (Py.GIL())
            {
                using var sys = Py.Import("sys");
                dynamic sysd = sys;
                sysd.path.insert(0, scriptDir);

                var mod = Py.Import("accel_server");
                if (mod == null)
                    throw new InvalidOperationException("无法导入 accel_server 模块。");

                if (!_callbackWired && LogSink != null)
                {
                    var cb = new Func<string, string, object?>((stream, msg) =>
                    {
                        try { LogSink(stream, msg); } catch (Exception) { /* 回调异常不能打断 Python */ }
                        return null;
                    });
                    using var pyCb = PyObject.FromManagedObject(cb);
                    dynamic modd = mod;
                    modd.set_log_callback(pyCb);
                    _callbackWired = true;
                }
                _module = mod;
                return mod;
            }
        }
    }

    /// <summary>模块的 dynamic 代理（调用 Python 函数走 DLR，自动管理 GIL 与参数转换）。</summary>
    public static dynamic Module
    {
        get
        {
            lock (Gate)
            {
                if (_module == null)
                    throw new InvalidOperationException("模块未加载，请先 GetModule。");
                return _module;
            }
        }
    }

    /// <summary>
    /// 在显式 GIL + Gate 保护下执行 Python 代码（统一 C#→Python 入口）。
    /// pythonnet DLR 的 TryGetMember（属性访问）不会自动持有 GIL，必须显式包 GIL。
    /// </summary>
    public static T WithGIL<T>(Func<dynamic, T> body)
    {
        lock (Gate)
        {
            if (_module == null)
                throw new InvalidOperationException("模块未加载，请先 GetModule。");
            using var gil = Py.GIL();
            return body(_module);
        }
    }

    /// <summary>无返回值版 WithGIL。</summary>
    public static void WithGIL(Action<dynamic> body)
    {
        lock (Gate)
        {
            if (_module == null)
                throw new InvalidOperationException("模块未加载，请先 GetModule。");
            using var gil = Py.GIL();
            body(_module);
        }
    }

    /// <summary>退出时清理引擎（幂等、尽力而为；服务停止由 MainWindow 负责）。</summary>
    public static void ShutdownIfNeeded()
    {
        lock (Gate)
        {
            if (!_initialized) return;
            try
            {
                if (_threadState != IntPtr.Zero)
                {
                    try { PythonEngine.EndAllowThreads(_threadState); } catch (Exception) { }
                }
                _module?.Dispose();
                _module = null;
                PythonEngine.Shutdown();
            }
            catch (Exception)
            {
                // 引擎关闭失败不阻塞程序退出
            }
            _initialized = false;
        }
    }
}
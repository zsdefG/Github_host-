using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Windows;
using WinsApp = System.Windows.Application;

namespace AccelGui;

public partial class App : WinsApp
{
    private const string SingleInstanceMutexName = "AccelGui_SingleInstance";
    private Mutex? _singleMutex;
    private bool _ownsMutex;

    protected override void OnStartup(StartupEventArgs e)
    {
        // 多开开关（设置 -> 允许同时打开多个程序窗口，默认关闭=单实例）
        if (!AppSettings.Load().AllowMultiple)
        {
            _singleMutex = new Mutex(true, SingleInstanceMutexName, out _ownsMutex);
            if (!_ownsMutex)
            {
                ActivateExistingInstance();
                Shutdown();       // 不调用 base.OnStartup，不创建第二个窗口
                return;
            }
        }
        base.OnStartup(e);
    }

    /// <summary>把已在前台运行的同名实例窗口调到前台并还原。</summary>
    private static void ActivateExistingInstance()
    {
        var me = Process.GetCurrentProcess();
        foreach (var p in Process.GetProcessesByName(me.ProcessName))
        {
            if (p.Id == me.Id) continue;
            try
            {
                if (p.MainWindowHandle != IntPtr.Zero && p.MainWindowTitle.Length > 0)
                {
                    if (IsIconic(p.MainWindowHandle)) ShowWindow(p.MainWindowHandle, SW_RESTORE);
                    SetForegroundWindow(p.MainWindowHandle);
                }
            }
            catch (Exception)
            {
                // 进程可能已退出，忽略
            }
            break;
        }
    }

    /// <summary>
    /// 关机 / 注销 / 重启时同步停止服务并还原系统代理。
    /// Windows 只给应用短暂响应时间（默认约 5 秒），超时后直接强杀进程；
    /// 若不在此同步清理，OnClosing 里"点 × 隐藏到托盘"的分支（e.Cancel=true）会
    /// 拦截 WPF 的 Shutdown，进程被强杀时 finally 与 Watchdog 都无法可靠兜底，
    /// 导致系统代理残留（下次开机所有流量仍指向已死的本地代理端口）。
    /// 这里只做清理、不取消会话（e.Cancel 保持 false），放行关机流程。
    /// </summary>
    protected override void OnSessionEnding(SessionEndingCancelEventArgs e)
    {
        try
        {
            (MainWindow as MainWindow)?.PrepareForSessionEnd();
        }
        catch (Exception)
        {
            // 清理失败不阻塞关机（尽力而为）
        }
        base.OnSessionEnding(e);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        // 兜底：确保 pythonnet 引擎退出时服务线程/代理残留被清理
        try
        {
            (MainWindow as MainWindow)?.StopAndCleanup();
        }
        catch (Exception)
        {
            // 窗口可能已销毁，忽略
        }
        PythonRuntime.ShutdownIfNeeded();
        try
        {
            if (_ownsMutex) _singleMutex?.ReleaseMutex();
        }
        catch (Exception)
        {
            // 互斥体释放失败不阻塞退出
        }
        base.OnExit(e);
    }

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    private const int SW_RESTORE = 9;
}
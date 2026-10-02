using System.ComponentModel;
using System.IO;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Documents;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Win32;
using NotifyIconAlias = System.Windows.Forms.NotifyIcon;
using TrayContextMenuAlias = System.Windows.Forms.ContextMenuStrip;
using TrayMenuItemAlias = System.Windows.Forms.ToolStripMenuItem;

namespace AccelGui;

public partial class MainWindow : Window
{
    private readonly AppSettings _settings = AppSettings.Load();
    private readonly DispatcherTimer _ipTimer = new() { Interval = TimeSpan.FromSeconds(5) };
    private readonly List<IpRow> _rows = new();

    // 运行状态
    private bool _running;
    private bool _isProxy;
    private int _activePort;        // 端口自适应后的实际端口
    private dynamic? _handle;   // start_dns/start_proxy 返回的 handle dict（dynamic）
    private bool _realExit;     // 托盘右键"彻底退出"：跳过隐藏逻辑，真正卸载
    private NotifyIconAlias? _trayIcon;
    private TrayContextMenuAlias? _trayMenu;

    private static readonly JsonSerializerOptions JsonOpts = new() { PropertyNameCaseInsensitive = true };

    public MainWindow()
    {
        InitializeComponent();
        ModeBox.SelectedIndex = 0;
        ApplyDisplayMode(_settings.DisplayMode);

        PythonRuntime.LogSink = AppendLog;
        _ipTimer.Tick += (_, _) => RefreshIpTable();
        _ipTimer.Start();
        InitTray();

        // 环境检测：不阻塞启动，后台线程收集 .NET 运行时/嵌入式 Python 状态后写日志
        System.Threading.ThreadPool.QueueUserWorkItem(_ => RunEnvCheck());
    }

    // ---------------- 环境检测（运行时 .NET / 嵌入式 Python） ----------------
    /// <summary>输出运行时环境摘要到日志；发现问题时在状态栏提示。</summary>
    private void RunEnvCheck()
    {
        var lines = new List<string>
        {
            $"[i] 运行时: {RuntimeInformation.FrameworkDescription} ({RuntimeInformation.ProcessArchitecture})",
        };

        // 部署形态：framework-dependent 需要目标机安装对应 WindowsDesktop 运行时；self-contained 已内嵌
        string baseDir = AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        bool selfContained = (AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES") as string ?? "")
            .Split(Path.PathSeparator)
            .Any(p => p.StartsWith(baseDir, StringComparison.OrdinalIgnoreCase)
                      && Path.GetFileName(p) is "PresentationFramework.dll" or "WindowsBase.dll");
        lines.Add(selfContained
            ? "[i] 部署形态: 自包含 (self-contained)，无需安装 .NET"
            : "[i] 部署形态: 框架依赖 (framework-dependent)，目标机需安装匹配的 .NET Desktop Runtime");

        // 系统已安装的 .NET (Core/5+) 运行时清单：
        // 优先文件系统（解压安装/共享框架真实位置），注册表登记缺失也不误报
        var runtimes = new List<string>();
        var sharedRoots = new List<string>();
        if (Environment.GetEnvironmentVariable("DOTNET_ROOT") is string dr && Directory.Exists(dr))
            sharedRoots.Add(Path.Combine(dr, "shared"));
        sharedRoots.Add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "dotnet", "shared"));
        if (RuntimeInformation.ProcessArchitecture == Architecture.X86)
            sharedRoots.Add(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), "dotnet", "shared"));
        foreach (var root in sharedRoots.Distinct())
        {
            if (!Directory.Exists(root)) continue;
            foreach (var fx in Directory.GetDirectories(root))
            {
                foreach (var ver in Directory.GetDirectories(fx))
                    runtimes.Add($"{Path.GetFileName(fx)} {Path.GetFileName(ver)}");
            }
        }
        // 补充注册表登记（安装器安装时的 InstalledVersions）
        foreach (var arch in new[] { "x64", "x86", "arm64" })
        {
            using var shared = Registry.LocalMachine.OpenSubKey($@"SOFTWARE\dotnet\Setup\InstalledVersions\{arch}\sharedfx");
            if (shared == null) continue;
            foreach (var name in shared.GetSubKeyNames())
            {
                if (shared.OpenSubKey(name)?.GetValue("Version") is string ver)
                    runtimes.Add($"{arch} {name} {ver}");
            }
        }
        runtimes = runtimes.Distinct().Order().ToList();
        lines.Add(runtimes.Count > 0
            ? $"[i] 系统已装 .NET 运行时: {string.Join(", ", runtimes)}"
            : "[i] 系统未检测到独立 .NET 运行时（当前进程由自带运行时承载，即 self-contained）");

        // 嵌入式 Python 运行时
        var embedded = Path.Combine(baseDir, "runtime", "python");
        bool embeddedOk = Directory.Exists(embedded)
                          && Directory.EnumerateFiles(embedded, "python*.dll").Any();
        lines.Add(embeddedOk
            ? $"[i] 嵌入式 Python: {embedded}（无需系统安装 Python）"
            : "[i] 嵌入式 Python: 未随程序打包，将回退系统 Python 探测（pythonnet）");

        string scriptDir = LocateScriptDir() ?? "";
        lines.Add(scriptDir.Length > 0
            ? $"[i] 加速脚本目录: {scriptDir}"
            : "[warn] 加速脚本目录: 未找到 accel_server.py，加速功能不可用！");

        // hosts 文件存在性（Steam++ NetworkEnvCheck 同款；Hosts 模式依赖它）
        string hostsPath = Path.Combine(Environment.SystemDirectory, "drivers", "etc", "hosts");
        bool hostsOk = File.Exists(hostsPath);
        lines.Add(hostsOk
            ? $"[i] hosts 文件: {hostsPath}"
            : $"[warn] 未找到 hosts 文件: {hostsPath}，Hosts 模式将无法写入！");

        // 物理网络接口可用性（排除虚拟/隧道网卡，参考 Steam++ CheckNetworkInterfaces）
        var usableIfaces = NetworkInterface.GetAllNetworkInterfaces()
            .Where(n => (n.NetworkInterfaceType == NetworkInterfaceType.Ethernet
                         || n.NetworkInterfaceType == NetworkInterfaceType.Wireless80211)
                        && n.OperationalStatus == OperationalStatus.Up
                        && !n.Description.Contains("Virtual", StringComparison.OrdinalIgnoreCase))
            .ToList();
        if (usableIfaces.Count > 0)
            lines.Add($"[i] 可用网络接口: {string.Join(", ", usableIfaces.Select(n => n.Name))}");
        else
            lines.Add("[warn] 未发现可用的以太网/无线网卡（可能被虚拟网卡遮挡或网卡未启用）");

        foreach (var line in lines) AppendLog("out", line);
        if (!embeddedOk) AppendLog("warn", "[warn] 建议在输出目录放置 runtime\\python（python*.dll + Lib）以支持无 Python 环境运行");
        if (scriptDir.Length == 0)
            SetBusy(false, "脚本缺失，无法启动");
    }

    // ---------------- 系统托盘（点 × 隐藏到托盘，右键可显示/退出） ----------------
    /// <summary>从当前 exe 提取内嵌图标（ApplicationIcon）；失败返回 null。</summary>
    private static System.Drawing.Icon? TryExtractExeIcon()
    {
        try
        {
            using var process = System.Diagnostics.Process.GetCurrentProcess();
            return System.Drawing.Icon.ExtractAssociatedIcon(process.MainModule?.FileName ?? "");
        }
        catch
        {
            return null;
        }
    }

    private void InitTray()
    {
        // 托盘图标来源优先级：程序集内嵌 AppIcon.ico（pack URI）→ exe 的 Win32 图标
        // （ApplicationIcon，ExtractAssociatedIcon 必然成功）→ 系统默认图标。
        // 不用 pack URI 也能拿到 GitHub 图标，避免资源加载异常时退回默认图标。
        System.Drawing.Icon? icon = null;
        try
        {
            using var stream = System.Windows.Application.GetResourceStream(
                new Uri("pack://application:,,,/AppIcon.ico"))?.Stream;
            if (stream != null)
                icon = new System.Drawing.Icon(stream);
        }
        catch
        {
            // 忽略，走下一步兜底
        }
        icon ??= TryExtractExeIcon();
        icon ??= System.Drawing.SystemIcons.Application;

        _trayMenu = new TrayContextMenuAlias();
        var showItem = new TrayMenuItemAlias("显示窗口") { Font = _trayMenu.Font };
        showItem.Click += (_, _) => ShowWindow();
        var exitItem = new TrayMenuItemAlias("退出") { Font = _trayMenu.Font };
        exitItem.Click += (_, _) => ExitApp();
        _trayMenu.Items.Add(showItem);
        _trayMenu.Items.Add(new System.Windows.Forms.ToolStripSeparator());
        _trayMenu.Items.Add(exitItem);

        _trayIcon = new NotifyIconAlias
        {
            Icon = icon,
            Text = "Accel Server 加速器",
            Visible = true,
            ContextMenuStrip = _trayMenu,
        };
        _trayIcon.DoubleClick += (_, _) => ShowWindow();
    }

    private void ShowWindow()
    {
        Show();
        if (WindowState == WindowState.Minimized)
            WindowState = WindowState.Normal;
        Activate();
    }

    private void ExitApp()
    {
        _realExit = true;
        Close();   // 触发 OnClosing -> 走真正的停止+卸载
    }

    protected override void OnClosing(CancelEventArgs e)
    {
        _ipTimer.Stop();
        if (!_realExit)
        {
            // 点 ×：隐藏到托盘（服务继续运行），不退出
            e.Cancel = true;
            Hide();
            return;
        }
        StopAndCleanup();
        _trayIcon?.Dispose();
        _trayMenu?.Dispose();
        base.OnClosing(e);
    }

    // ---------------- 显示模式 ----------------
    private void ApplyDisplayMode(DisplayMode mode)
    {
        bool config = mode != DisplayMode.Minimal;
        bool table = mode == DisplayMode.Full;
        ConfigPanel.Visibility = config ? Visibility.Visible : Visibility.Collapsed;
        ConnectTestBtn.Visibility = config ? Visibility.Visible : Visibility.Collapsed;
        IpHeader.Visibility = table ? Visibility.Visible : Visibility.Collapsed;
        RefreshBtn.Visibility = table ? Visibility.Visible : Visibility.Collapsed;
        IpGrid.Visibility = table ? Visibility.Visible : Visibility.Collapsed;
        ModeLabel.Text = mode switch
        {
            DisplayMode.Minimal => "显示模式：精简",
            DisplayMode.Full => "显示模式：完整",
            _ => "显示模式：标准",
        };
    }

    private void OnSettings(object sender, RoutedEventArgs e)
    {
        var dlg = new SettingsDialog(_settings) { Owner = this };
        if (dlg.ShowDialog() == true)
        {
            _settings.Save();
            ApplyDisplayMode(_settings.DisplayMode);
        }
    }

    // ---------------- 模式切换 ----------------
    private void OnModeChanged(object sender, RoutedEventArgs e)
    {
        bool isProxy = ModeBox.SelectedIndex == 1;
        AutoProxyBox.IsEnabled = isProxy;
        UpstreamBox.IsEnabled = !isProxy;
        // 仅当端口还是默认值时跟随模式切换默认端口
        if (PortBox.Text == "5353" || PortBox.Text == "8080")
            PortBox.Text = isProxy ? "8080" : "5353";
    }

    // ---------------- 启动 / 停止 ----------------
    private async void OnStartStop(object sender, RoutedEventArgs e)
    {
        if (_running) { await StopAsync(); return; }
        await StartAsync();
    }

    private async Task StartAsync()
    {
        if (!int.TryParse(PortBox.Text.Trim(), out var port) || port < 0 || port > 65535)
        {
            MessageBox.Show(this, "端口必须是 0-65535 的整数。", "参数错误", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }
        if (!double.TryParse(RecheckBox.Text.Trim(), out var recheck) || recheck < 0)
        {
            MessageBox.Show(this, "复检间隔必须是 ≥0 的秒数（0 = 关闭复检）。", "参数错误", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }
        string upstream = UpstreamBox.Text.Trim();
        if (string.IsNullOrEmpty(upstream))
            upstream = "223.5.5.5";

        SetBusy(true, "正在初始化 Python 引擎…");
        try
        {
            PythonRuntime.EnsureInitialized(string.IsNullOrWhiteSpace(_settings.PythonDll) ? null : _settings.PythonDll);
            var scriptDir = LocateScriptDir()
                ?? throw new InvalidOperationException("找不到 accel_server.py，请确认其与程序在同一目录树（hosts 目录）。");
            PythonRuntime.GetModule(scriptDir); // 加载模块 + 挂日志回调

            SetBusy(true, "正在解析域名并做健康检测（首次需 5-30 秒）…");
            int count = await Task.Run(() => PythonRuntime.WithGIL(m => (int)m.refresh_ip_map()));
            if (count == 0)
            {
                MessageBox.Show(this, "没有任何可加速的 IP（全部解析失败），请检查网络后重试。", "启动失败",
                                MessageBoxButton.OK, MessageBoxImage.Warning);
                SetBusy(false, "未启动");
                return;
            }

            _isProxy = ModeBox.SelectedIndex == 1;
            bool setProxy = AutoProxyBox.IsChecked == true;
            bool useDoh = _settings.UseDoh;
            string dohEndpoint = _settings.DohEndpoint.Trim();

            // DoH 端点留空 = 自动选择：并行实测内置清单，挑最快可用端点
            if (useDoh && !_isProxy && string.IsNullOrEmpty(dohEndpoint))
            {
                SetBusy(true, "正在自动选择最快的 DoH 端点…");
                (string url, double secs) = await Task.Run(() =>
                {
                    return PythonRuntime.WithGIL(m =>
                    {
                        dynamic r = m.detect_best_doh(null, 3.0);
                        return ((string)r[0], (double)r[1]);
                    });
                });
                if (!string.IsNullOrEmpty(url))
                {
                    dohEndpoint = url;
                    AppendLog("out", $"[i] 已自动选择 DoH 端点：{url}（实测 {(int)(secs * 1000)}ms）");
                }
                else
                {
                    useDoh = false;
                    AppendLog("warn", "[warn] 所有 DoH 端点探测失败（网络不可达），本次回退 UDP 上游");
                }
            }

            SetBusy(true, _isProxy ? "正在启动 HTTP 加速代理…" : "正在启动 DNS 加速服务…");
            _handle = await Task.Run(() => PythonRuntime.WithGIL(m =>
            {
                dynamic h = _isProxy
                    ? m.start_proxy(port, m.IP_MAP, setProxy, recheck)
                    : m.start_dns(port, upstream, m.IP_MAP, recheck, 2.0, useDoh, dohEndpoint);
                _activePort = (int)h["port"];   // 端口自适应后的实际端口
                return h;
            }));

            _running = true;
            StartBtn.Content = "停止";
            if (_activePort != port)  // 端口被占用自动更换后，回写输入框与日志
            {
                PortBox.Text = _activePort.ToString();
                AppendLog("out", $"[i] 端口 {port} 已被占用，已自动改用 {_activePort}");
            }
            SetBusy(false, _isProxy ? $"代理已启动：127.0.0.1:{_activePort}" : $"DNS 已启动：127.0.0.1:{_activePort}");
            if (!_isProxy && useDoh) AppendLog("out", $"[i] DoH 上游已启用：{dohEndpoint}（失败自动回退 UDP {upstream}）");
            if (_isProxy && setProxy) AppendLog("out", "[i] 系统代理已设置（停止服务/关闭程序时自动还原）");
        }
        catch (Exception ex)
        {
            AppendLog("err", $"[!] 启动失败: {ex.Message}");
            MessageBox.Show(this, ex.Message, "启动失败", MessageBoxButton.OK, MessageBoxImage.Error);
            SetBusy(false, "未启动");
        }
    }

    private async Task StopAsync()
    {
        var handle = _handle;
        _handle = null;
        if (handle == null) return;
        var isProxy = _isProxy;

        SetBusy(true, "正在停止并还原系统代理…");
        try
        {
            await Task.Run(() => PythonRuntime.WithGIL(m =>
            {
                if (isProxy) m.stop_proxy(handle); else m.stop_dns(handle);
            }));
        }
        catch (Exception ex)
        {
            AppendLog("err", $"[!] 停止时异常: {ex.Message}");
        }
        finally
        {
            _running = false;
            StartBtn.Content = "启动";
            SetBusy(false, "已停止");
        }
    }

    private void StopAndCleanup()
    {
        if (_handle != null)
        {
            try
            {
                PythonRuntime.WithGIL(m =>
                {
                    if (_isProxy) m.stop_proxy(_handle); else m.stop_dns(_handle);
                });
            }
            catch (Exception)
            {
            }
            _handle = null;
            _running = false;
        }
    }

    // ---------------- 连接测试 ----------------
    private async void OnConnectTest(object sender, RoutedEventArgs e)
    {
        try
        {
            if (!PythonRuntime.IsReady)
            {
                AppendLog("out", "[i] 正在初始化 Python 引擎…");
                await Task.Run(() =>
                {
                    PythonRuntime.EnsureInitialized(string.IsNullOrWhiteSpace(_settings.PythonDll) ? null : _settings.PythonDll);
                    var scriptDir = LocateScriptDir()
                        ?? throw new InvalidOperationException("找不到 accel_server.py，请确认其与程序在同一目录树（hosts 目录）。");
                    PythonRuntime.GetModule(scriptDir);
                });
            }
        }
        catch (Exception ex)
        {
            AppendLog("err", $"[!] 连接测试失败: {ex.Message}");
            MessageBox.Show(this, ex.Message, "连接测试失败", MessageBoxButton.OK, MessageBoxImage.Error);
            return;
        }

        ConnectTestBtn.IsEnabled = false;
        try
        {
            AppendLog("out", "[i] 连接测试开始：对默认加速域名探测 TCP 443 连通性…");
            string json = await Task.Run(() => PythonRuntime.WithGIL(m => (string)m.connection_test()));
            var result = JsonSerializer.Deserialize<ConnTestResult>(json, JsonOpts) ?? new ConnTestResult();
            AppendLog("out", $"[i] 连接测试完成（耗时 {result.Elapsed:0.0} 秒）：");
            foreach (var d in result.Domains)
            {
                string parts = string.Join("  ", d.Ips.Select(i =>
                    i.Latency.HasValue ? $"{i.Ip}:{i.Latency:0.0}ms" : $"{i.Ip}:不通"));
                AppendLog("out", $"    {d.Domain}  [{d.Ok}/{d.Total} 通]  {parts}");
            }

            if (_running)
            {
                if (_isProxy) AppendLog("out", await TestProxyChainAsync());
                else AppendLog("out", await TestDnsChainAsync());
            }
            else
            {
                AppendLog("out", "[i] 服务未运行，跳过本地链路验证（启动服务后可再测）。");
            }
        }
        catch (Exception ex)
        {
            AppendLog("err", $"[!] 连接测试异常: {ex.Message}");
        }
        finally
        {
            ConnectTestBtn.IsEnabled = true;
        }
    }

    /// <summary>DNS 链路验证：向本机 DNS 服务发 github.com A 查询，校验返回 IP 是否命中加速清单。</summary>
    private async Task<string> TestDnsChainAsync()
    {
        var query = BuildDnsQuery("github.com");
        using var udp = new UdpClient();
        var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        try
        {
            await udp.SendAsync(query, query.Length, "127.0.0.1", _activePort);
            var recv = await udp.ReceiveAsync(cts.Token);
            var ips = ParseDnsAnswers(recv.Buffer, query.Length); // qend = 12 + qname + 4 = 整个查询长度
            if (ips.Count == 0)
                return "[链] DNS 链路验证失败：未返回 A 记录";

            var snapshot = PythonRuntime.WithGIL(m => (string)m.snapshot_ip_map_json());
            var map = JsonSerializer.Deserialize<Dictionary<string, List<string>>>(snapshot, JsonOpts)
                      ?? new Dictionary<string, List<string>>();
            var accel = map.GetValueOrDefault("github.com", new List<string>());
            int hit = accel.Count(ips.Contains);
            string hitPart = hit == ips.Count ? "命中加速 IP"
                : hit > 0 ? "部分命中（其余可能已被复检更新）"
                : "未命中（可能经上游转发，或加速 IP 已更新）";
            return $"[链] DNS 链路验证通过：127.0.0.1:{_activePort} 返回 A 记录 {string.Join(",", ips)}（{hitPart}）";
        }
        catch (OperationCanceledException)
        {
            return "[链] DNS 链路验证失败：请求超时（无响应）";
        }
        catch (Exception ex)
        {
            return $"[链] DNS 链路验证失败：{ex.Message}";
        }
    }

    /// <summary>Proxy 链路验证：向本机代理发 CONNECT github.com:443，期望 200。</summary>
    private async Task<string> TestProxyChainAsync()
    {
        using var client = new TcpClient();
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(8));
            await client.ConnectAsync("127.0.0.1", _activePort, cts.Token);
            await using var stream = client.GetStream();
            var req = Encoding.ASCII.GetBytes(
                "CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n");
            await stream.WriteAsync(req, cts.Token);
            var buf = new byte[256];
            int n = await stream.ReadAsync(buf, cts.Token);
            var resp = Encoding.ASCII.GetString(buf, 0, n);
            bool ok = resp.StartsWith("HTTP/1.1 200", StringComparison.OrdinalIgnoreCase)
                      || resp.StartsWith("HTTP/1.0 200", StringComparison.OrdinalIgnoreCase);
            return ok
                ? $"[链] Proxy 链路验证通过：127.0.0.1:{_activePort} CONNECT github.com:443 → 200"
                : $"[链] Proxy 链路验证失败：{resp.Split('\r', '\n')[0]}";
        }
        catch (Exception ex)
        {
            return $"[链] Proxy 链路验证失败：{ex.Message}";
        }
    }

    /// <summary>构造 A 记录 DNS 查询（github.com）。</summary>
    private static byte[] BuildDnsQuery(string domain)
    {
        var labels = domain.Split('.');
        // qname 字节数 = 域名文本 - 点号 + 每标签 1 个长度字节 + 1 个终止字节 = domain.Length + 2
        int qnameLen = domain.Length + 2;
        var q = new byte[12 + qnameLen + 4];
        q[0] = 0x12; q[1] = 0x34;   // ID
        q[2] = 0x01;                // RD=1
        q[5] = 0x01;                // QDCOUNT=1
        int off = 12;
        foreach (var label in labels)
        {
            q[off++] = (byte)label.Length;
            foreach (byte ch in Encoding.ASCII.GetBytes(label)) q[off++] = ch;
        }
        q[off++] = 0;               // 根标签
        q[off++] = 0; q[off++] = 1; // A
        q[off++] = 0; q[off++] = 1; // IN
        return q;
    }

    /// <summary>解析 DNS 响应里的 A 记录 IP 列表（跳过名字字段，支持压缩指针/标签两种形式）。</summary>
    private static List<string> ParseDnsAnswers(byte[] resp, int qend)
    {
        var ips = new List<string>();
        if (resp.Length < qend + 6) return ips;
        int ancount = (resp[6] << 8) | resp[7];
        int off = qend;
        for (int i = 0; i < ancount && off + 12 <= resp.Length; i++)
        {
            int p = off;
            if ((resp[p] & 0xC0) == 0xC0) p += 2;              // 压缩指针
            else
            {
                while (p < resp.Length && resp[p] != 0) p += resp[p] + 1;
                p += 1;                                        // 终止 0
            }
            if (p + 10 > resp.Length) break;
            int type = (resp[p] << 8) | resp[p + 1];
            int rdlen = (resp[p + 8] << 8) | resp[p + 9];
            if (type == 1 && rdlen == 4 && p + 10 + 4 <= resp.Length)
                ips.Add($"{resp[p + 10]}.{resp[p + 11]}.{resp[p + 12]}.{resp[p + 13]}");
            off = p + 10 + rdlen;
        }
        return ips;
    }

    // ---------------- IP 表 ----------------
    private void OnRefresh(object sender, RoutedEventArgs e) => RefreshIpTable();

    private async void RefreshIpTable()
    {
        if (_settings.DisplayMode != DisplayMode.Full) return;
        try
        {
            string json = await Task.Run(() => PythonRuntime.WithGIL(m => (string)m.snapshot_ip_map_json()));
            var dict = JsonSerializer.Deserialize<Dictionary<string, List<string>>>(json)
                       ?? new Dictionary<string, List<string>>();
            _rows.Clear();
            foreach (var kv in dict.OrderBy(k => k.Key))
                _rows.Add(new IpRow(kv.Key, kv.Value));
            if (IpGrid.ItemsSource == null)
                IpGrid.ItemsSource = _rows;
        }
        catch (Exception)
        {
            // 引擎未初始化/服务未启动时静默跳过
        }
    }

    // ---------------- 日志 ----------------
    private void AppendLog(string stream, string msg)
    {
        var dispatcher = LogBox.Dispatcher;
        dispatcher.BeginInvoke(new Action(() =>
        {
            var run = new Run(msg + Environment.NewLine);
            if (stream == "err")
                run.Foreground = Brushes.DarkRed;
            LogBox.Document.Blocks.Add(new Paragraph(run));
            if (LogBox.Document.Blocks.Count > 2000)
                LogBox.Document.Blocks.Remove(LogBox.Document.Blocks.FirstBlock);
            LogBox.ScrollToEnd();
        }));
    }

    // ---------------- 状态指示 ----------------
    private void SetBusy(bool busy, string text)
    {
        StartBtn.IsEnabled = !busy;
        StatusText.Text = text;
        StatusDot.Fill = busy ? Brushes.Orange
                        : _running ? Brushes.Green
                        : Brushes.Gray;
    }

    // ---------------- 脚本定位（不写死路径） ----------------
    private static string? LocateScriptDir()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir != null)
        {
            if (File.Exists(Path.Combine(dir.FullName, "accel_server.py")))
                return dir.FullName;
            if (File.Exists(Path.Combine(dir.FullName, "hosts", "accel_server.py")))
                return Path.Combine(dir.FullName, "hosts");
            dir = dir.Parent;
        }
        return null;
    }
}

/// <summary>connection_test 返回的 JSON 结构模型。</summary>
internal sealed class ConnTestResult
{
    public double Elapsed { get; set; }
    public List<ConnTestDomain> Domains { get; set; } = new();
}

internal sealed class ConnTestDomain
{
    public string Domain { get; set; } = "";
    public int Ok { get; set; }
    public int Total { get; set; }
    public List<ConnTestIp> Ips { get; set; } = new();
}

internal sealed class ConnTestIp
{
    public string Ip { get; set; } = "";
    public double? Latency { get; set; }
}
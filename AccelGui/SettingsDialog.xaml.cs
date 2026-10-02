using System.Windows;
using Microsoft.Win32;

namespace AccelGui;

public partial class SettingsDialog : Window
{
    // 内置 DoH 端点（与 accel_server.py 的 DOH_SERVERS 保持一致）
    internal static readonly (string Name, string Url)[] BuiltinDohServers =
    {
        ("腾讯 DNSPod 1.12.12.12", "https://1.12.12.12/resolve"),
        ("DNSPod doh.pub", "https://doh.pub/resolve"),
        ("阿里 120.53.53.53", "https://120.53.53.53/resolve"),
        ("阿里 dns.alidns.com", "https://dns.alidns.com/resolve"),
        ("阿里 223.6.6.6", "https://223.6.6.6/resolve"),
        ("阿里 223.5.5.5", "https://223.5.5.5/resolve"),
        ("Google dns.google", "https://dns.google/resolve"),
        ("Cloudflare cloudflare-dns.com", "https://cloudflare-dns.com/resolve"),
        ("360 doh.360.cn", "https://doh.360.cn/resolve"),
        ("清华 TUNA 101.6.6.6:8443", "https://101.6.6.6:8443/resolve"),
    };

    private readonly AppSettings _settings;

    public SettingsDialog(AppSettings settings)
    {
        InitializeComponent();
        _settings = settings;
        switch (settings.DisplayMode)
        {
            case DisplayMode.Minimal: ModeMinimal.IsChecked = true; break;
            case DisplayMode.Full: ModeFull.IsChecked = true; break;
            default: ModeStandard.IsChecked = true; break;
        }
        PythonDllBox.Text = settings.PythonDll;

        foreach (var (name, url) in BuiltinDohServers)
            DohServerBox.Items.Add(name);
        // 首项"自动选择"：留空端点，启动时程序实测内置清单自动挑最快
        DohServerBox.Items.Insert(0, "自动选择（推荐）");
        DohCustomBox.Text = settings.DohEndpoint;
        if (string.IsNullOrWhiteSpace(settings.DohEndpoint))
        {
            DohServerBox.SelectedIndex = 0;  // 空端点 = 自动选择
        }
        else
        {
            int idx = Array.FindIndex(BuiltinDohServers, s => s.Url.Equals(settings.DohEndpoint, StringComparison.OrdinalIgnoreCase));
            if (idx >= 0) DohServerBox.SelectedIndex = idx + 1;  // 内置项自 2 号起
        }
        DohEnableBox.IsChecked = settings.UseDoh;
        AllowMultipleBox.IsChecked = settings.AllowMultiple;
        UpdateDohEnabled();
    }

    private void UpdateDohEnabled()
    {
        bool on = DohEnableBox.IsChecked == true;
        DohServerBox.IsEnabled = on;
        DohCustomBox.IsEnabled = on;
    }

    private void OnDohToggled(object sender, RoutedEventArgs e) => UpdateDohEnabled();

    private void OnDohSelected(object sender, System.Windows.Controls.SelectionChangedEventArgs e)
    {
        // 索引 0 = 自动选择（推荐）：清空端点，启动时自动实测挑最快
        if (DohServerBox.SelectedIndex <= 0)
        {
            DohCustomBox.Text = "";
            return;
        }
        int idx = DohServerBox.SelectedIndex - 1;
        if (idx >= 0 && idx < BuiltinDohServers.Length)
            DohCustomBox.Text = BuiltinDohServers[idx].Url;
    }

    private void OnBrowse(object sender, RoutedEventArgs e)
    {
        var dlg = new OpenFileDialog
        {
            Title = "选择 python DLL（python312.dll 等）",
            Filter = "Python DLL|python*.dll|所有文件|*.*",
        };
        if (dlg.ShowDialog(this) == true)
            PythonDllBox.Text = dlg.FileName;
    }

    private void OnSave(object sender, RoutedEventArgs e)
    {
        if (ModeMinimal.IsChecked == true) _settings.DisplayMode = DisplayMode.Minimal;
        else if (ModeFull.IsChecked == true) _settings.DisplayMode = DisplayMode.Full;
        else _settings.DisplayMode = DisplayMode.Standard;

        var dll = PythonDllBox.Text.Trim();
        if (!string.IsNullOrEmpty(dll) && !System.IO.File.Exists(dll))
        {
            MessageBox.Show(this, $"文件不存在：{dll}\n\n留空将自动探测。", "路径无效",
                            MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }
        _settings.PythonDll = dll;

        _settings.UseDoh = DohEnableBox.IsChecked == true;
        _settings.DohEndpoint = DohCustomBox.Text.Trim();
        _settings.AllowMultiple = AllowMultipleBox.IsChecked == true;

        DialogResult = true;
    }
}
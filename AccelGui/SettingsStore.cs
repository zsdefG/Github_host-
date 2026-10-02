using System.IO;
using System.Text.Json;

namespace AccelGui;

/// <summary>显示模式：精简 / 标准 / 完整。</summary>
public enum DisplayMode
{
    Minimal = 0,
    Standard = 1,
    Full = 2,
}

/// <summary>应用设置持久化（JSON，存 %APPDATA%\AccelGui\settings.json）。</summary>
public sealed class AppSettings
{
    public DisplayMode DisplayMode { get; set; } = DisplayMode.Standard;
    public string PythonDll { get; set; } = "";
    public bool UseDoh { get; set; } = true;        // DNS 模式启用 DoH 上游（默认开启）
    public string DohEndpoint { get; set; } = "";   // DoH 端点 URL（如 https://dns.alidns.com/resolve）；留空=启动时自动选最快端点
    public bool AllowMultiple { get; set; }         // 是否允许打开多个程序窗口（默认 false=单实例）

    private static readonly JsonSerializerOptions JsonOpts = new() { WriteIndented = true };
    private static string StorePath =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
                     "AccelGui", "settings.json");

    public static AppSettings Load()
    {
        try
        {
            if (File.Exists(StorePath))
                return JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(StorePath)) ?? new AppSettings();
        }
        catch (Exception)
        {
            // 配置文件损坏时回退默认
        }
        return new AppSettings();
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(StorePath)!);
            File.WriteAllText(StorePath, JsonSerializer.Serialize(this, JsonOpts));
        }
        catch (Exception)
        {
            // 保存失败不致命
        }
    }
}
namespace AccelGui;

/// <summary>IP 表行（完整显示模式 DataGrid 绑定，每次刷新重建列表）。</summary>
public sealed class IpRow
{
    public string Domain { get; }
    public string IpText { get; }
    public string Count { get; }

    public IpRow(string domain, IReadOnlyList<string> ips)
    {
        Domain = domain;
        IpText = string.Join(", ", ips);
        Count = $"{ips.Count} 个";
    }
}
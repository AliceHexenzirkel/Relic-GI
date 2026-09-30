using System.Net.Http;
using System.Text;
using System.Text.Json;
using Relic.Core.Util;

namespace Relic.Core.Server;

/// <summary>
/// Token-less client for the agent's PUBLIC endpoints (Player mode): <c>/health</c>,
/// <c>/public/status</c>, <c>/public/account/create</c> + <c>/public/jobs/&lt;id&gt;</c>,
/// <c>/public/command</c>. Direct HTTP only (no SSH), never through the system proxy (Fiddler may be
/// running), bodies always counted (the agent reads Content-Length). Every failure surfaces as an
/// <see cref="AgentPublicException"/> with the HTTP status, the agent's stable error code (agent 3.2+) and
/// a message: the translation of that code when this build knows it, else the agent's own text.
/// </summary>
public sealed class AgentPublicClient : IDisposable
{
    private readonly HttpClient _http;
    public string Host { get; }
    public int Port { get; }

    public AgentPublicClient(string host, int port = ServerAddress.DefaultAgentPort, TimeSpan? timeout = null)
    {
        Host = host; Port = port;
        string h = host.Contains(':') && !host.StartsWith('[') ? "[" + host + "]" : host;
        _http = new HttpClient(new SocketsHttpHandler { UseProxy = false, ConnectTimeout = TimeSpan.FromSeconds(6) })
        {
            BaseAddress = new Uri($"http://{h}:{port}/"),
            Timeout = timeout ?? TimeSpan.FromSeconds(20),
        };
        _http.DefaultRequestHeaders.UserAgent.ParseAdd("Relic/3 (player)");
    }

    public Task<JsonElement> HealthAsync(CancellationToken ct = default) => GetAsync("health", ct);
    public Task<JsonElement> StatusAsync(CancellationToken ct = default) => GetAsync("public/status", ct);

    /// <summary>Start a signup job: returns the public job id. <paramref name="client"/> = this launcher's id on
    /// that server (<see cref="State.RelicState.ClientIdFor"/>): agent 3.7 counts its accounts by it against the
    /// version's <c>maxPerPlayer</c>; an older agent ignores the field.</summary>
    public async Task<string> CreateAccountAsync(string version, string name, string? password, string template, string? client = null, CancellationToken ct = default)
    {
        var body = new Dictionary<string, object?> { ["version"] = version, ["name"] = name, ["template"] = template };
        if (!string.IsNullOrEmpty(password)) body["password"] = password;
        if (!string.IsNullOrEmpty(client)) body["client"] = client;
        var res = await PostAsync("public/account/create", body, ct);
        return res.TryGetProperty("job", out var j) && j.ValueKind == JsonValueKind.String ? j.GetString()! : "";
    }

    public Task<JsonElement> JobAsync(string id, CancellationToken ct = default) => GetAsync("public/jobs/" + Uri.EscapeDataString(id), ct);

    public Task<JsonElement> CommandAsync(string version, string uid, string msg, CancellationToken ct = default)
        => PostAsync("public/command", new { version, uid, msg }, ct);

    /// <summary>Create an account and follow the job to completion. Lines go to <paramref name="onLine"/>;
    /// only a busy job slot (<see cref="IsBusyRetry"/>) is retried, with backoff for up to ~2 minutes —
    /// a quota or rate-limit refusal surfaces at once with its real wait. Returns the job result object.</summary>
    public async Task<JsonElement> CreateAccountAndWaitAsync(string version, string name, string? password, string template,
        Action<string>? onLine = null, CancellationToken ct = default, string? client = null)
    {
        string jobId = "";
        var deadline = DateTime.UtcNow.AddMinutes(2);
        int attempt = 0;
        while (true)
        {
            try { jobId = await CreateAccountAsync(version, name, password, template, client, ct); break; }
            catch (AgentPublicException ex) when (IsBusyRetry(ex) && DateTime.UtcNow < deadline)
            {
                int wait = Math.Clamp(ex.RetryAfter ?? (5 * ++attempt), 2, 30);
                onLine?.Invoke(L.T("core.agent.busyRetry", new { seconds = wait }));
                await Task.Delay(TimeSpan.FromSeconds(wait), ct);
            }
        }
        if (jobId.Length == 0) throw new AgentPublicException(500, L.T("core.agent.noJobId"));
        int failures = 0;
        while (true)
        {
            await Task.Delay(1000, ct);
            JsonElement snap;
            try { snap = await JobAsync(jobId, ct); failures = 0; }
            catch (AgentPublicException ex) when (ex.Status == 404) { throw; }
            catch (Exception) when (++failures < 10) { continue; }
            string state = snap.TryGetProperty("state", out var s) ? s.GetString() ?? "" : "";
            if (state == "error")
            {
                string raw = snap.TryGetProperty("error", out var e) ? e.GetString() ?? "error" : "error";
                string? code = StringProp(snap, "errorCode");
                throw new AgentPublicException(500, Describe(code, null, raw), null, code);
            }
            if (state != "running")
                return snap.TryGetProperty("result", out var r) ? r.Clone() : snap.Clone();
        }
    }

    /// <summary>The one refusal worth waiting out: the agent's single job slot is taken. Agent 3.2+ says so
    /// with <c>server_busy</c>; an older agent sends no code, so its busy 429 is recognised by its text plus
    /// <c>retryAfter: 30</c> — never by the wait alone: a quota wait is the time to the next token and can
    /// be as short as 1 s.</summary>
    public static bool IsBusyRetry(AgentPublicException ex)
        => ex.Status == 429 && (ex.Code == "server_busy"
            || (ex.Code is null && ex.RetryAfter == 30
                && ex.Message.StartsWith("the server is busy", StringComparison.OrdinalIgnoreCase)));

    /// <summary>The player-facing text of a public refusal: <c>core.agent.err.&lt;code&gt;</c> (with
    /// <c>{wait}</c> from Retry-After) when this build knows the code, else the agent's own message — so an
    /// unknown code from a newer agent, or a wait the agent did not send, still reads sensibly.</summary>
    public static string Describe(string? code, int? retryAfter, string agentMessage)
    {
        if (string.IsNullOrEmpty(code) || !code.All(c => c is (>= 'a' and <= 'z') or (>= '0' and <= '9') or '_'))
            return agentMessage;
        string key = "core.agent.err." + code;
        if (!L.Has(key)) return agentMessage;
        if (!L.T(key).Contains("{wait}")) return L.T(key);
        return retryAfter is int s && s > 0 ? L.T(key, new { wait = FormatWait(s) }) : agentMessage;
    }

    /// <summary>A Retry-After in words, rounded UP so the player is never told to retry before the agent
    /// would admit them: under 90 s in seconds, under 90 min in minutes, else hours.</summary>
    public static string FormatWait(int seconds)
    {
        seconds = Math.Max(1, seconds);
        if (seconds < 90) return L.T("core.install.time.sec", new { n = seconds });
        if (seconds < 90 * 60) return L.T("core.install.time.min", new { n = (seconds + 59) / 60 });
        return L.T("core.agent.time.hours", new { n = (seconds + 3599) / 3600 });
    }

    private static string? StringProp(JsonElement el, string name)
        => el.ValueKind == JsonValueKind.Object && el.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String
            ? v.GetString() : null;

    private async Task<JsonElement> GetAsync(string path, CancellationToken ct)
    {
        using var resp = await _http.GetAsync(path, ct);
        return await ReadJson(resp, ct);
    }

    private async Task<JsonElement> PostAsync(string path, object body, CancellationToken ct)
    {
        // Serialized up front into a counted StringContent: the agent reads Content-Length only.
        var json = JsonSerializer.Serialize(body);
        using var content = new StringContent(json, Encoding.UTF8, "application/json");
        using var resp = await _http.PostAsync(path, content, ct);
        return await ReadJson(resp, ct);
    }

    private static async Task<JsonElement> ReadJson(HttpResponseMessage resp, CancellationToken ct)
    {
        string text = await resp.Content.ReadAsStringAsync(ct);
        JsonElement el = default;
        bool parsed = false;
        try { using var doc = JsonDocument.Parse(text); el = doc.RootElement.Clone(); parsed = true; } catch { }
        if (!resp.IsSuccessStatusCode)
        {
            string msg = parsed && el.ValueKind == JsonValueKind.Object && el.TryGetProperty("error", out var e) && e.ValueKind == JsonValueKind.String
                ? e.GetString()! : $"HTTP {(int)resp.StatusCode}";
            int? retry = null;
            if (parsed && el.ValueKind == JsonValueKind.Object && el.TryGetProperty("retryAfter", out var ra) && ra.TryGetInt32(out int rv)) retry = rv;
            else if (resp.Headers.RetryAfter?.Delta is TimeSpan d) retry = (int)d.TotalSeconds;
            string? code = parsed ? StringProp(el, "code") : null;
            throw new AgentPublicException((int)resp.StatusCode, Describe(code, retry, msg), retry, code);
        }
        if (!parsed) throw new AgentPublicException(502, L.T("core.agent.notJson"));
        return el;
    }

    public void Dispose() => _http.Dispose();
}

public sealed class AgentPublicException : Exception
{
    public int Status { get; }
    public int? RetryAfter { get; }
    /// <summary>The agent's stable reason (docs/AGENT-API.md "Public error codes"); null from an agent older than 3.2.</summary>
    public string? Code { get; }
    public AgentPublicException(int status, string message, int? retryAfter = null, string? code = null) : base(message)
    { Status = status; RetryAfter = retryAfter; Code = code; }
}

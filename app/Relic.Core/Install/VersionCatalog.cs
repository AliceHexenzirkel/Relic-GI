using System.Text.Json;
using Relic.Core.Util;

namespace Relic.Core.Install;

/// <summary>
/// An alternative host for the same archive — Internet Archive today, anything else tomorrow.
/// </summary>
/// <remarks>
/// <see cref="Size"/> is per-mirror on purpose, and it is the whole reason this is a type rather than
/// a bare url list: a mirror is not necessarily the same BUILD as <see cref="GameSourceInfo.CdnUrl"/>.
/// That is not hypothetical — it is how this type came to exist. On 2026-08-19 the 1.6 entry pointed at
/// the CDN's <c>GenshinImpact_1.6.0.zip</c> while Drive and the archive.org mirror both served
/// <c>1.6.1</c>, exactly 16.384 bytes shorter (head, 1 GB mark and trailing EOCD compared live on all
/// three). Against a single catalogue-wide size, a FINISHED 15 GB download from such a host is called
/// "incomplete", deleted and restarted, forever. The 1.6 entry has since been re-pointed at 1.6.1 on
/// every host, but a mirror is still free to diverge and must carry its own byte count.
/// </remarks>
public sealed class GameMirrorInfo
{
    /// <summary>Stable key stored in <c>RelicSettings.Source</c>; must match across versions that
    /// carry the same mirror. A version without it simply falls back to the CDN.</summary>
    public string Id { get; set; } = "";
    /// <summary>Shown in the UI (wizard + Settings).</summary>
    public string Label { get; set; } = "";
    /// <summary>One-line hint under the label in the wizard. Lives here rather than in the
    /// UI so a new mirror needs no code change — and so what the user is told about a host's speed
    /// stays next to that host's url.</summary>
    public string Note { get; set; } = "";
    public string Url { get; set; } = "";
    public long Size { get; set; }
}

public sealed class GameSourceInfo
{
    public string CdnUrl { get; set; } = "";
    public string? DriveId { get; set; }
    public long Size { get; set; }
    public List<GameMirrorInfo> Mirrors { get; set; } = new();

    /// <summary>Does at least one host exist to pull this archive from? A pack with none would only
    /// ever surface as <c>core.download.noSource</c> at install time — hours after the wizard offered it.</summary>
    public bool HasAnySource =>
        !string.IsNullOrWhiteSpace(CdnUrl)
        || !string.IsNullOrWhiteSpace(DriveId)
        || (Mirrors?.Any(m => !string.IsNullOrWhiteSpace(m.Url) && !string.IsNullOrWhiteSpace(m.Id)) ?? false);
}

/// <summary>
/// The spoken-voice languages the client ships as separate packs, and the one vocabulary Relic uses for
/// them end to end. The names are the release index's spelling on purpose — <c>English(US)</c>,
/// <c>Chinese</c>, <c>Japanese</c>, <c>Korean</c> — because that spelling IS the on-disk folder
/// (<c>GeneratedSoundBanks/Windows/&lt;Lang&gt;</c>), the marker file (<c>Audio_&lt;Lang&gt;_pkg_version</c>),
/// the line in the client's <c>audio_lang_14</c>, the CDN file name and the agent's <c>hotpatch.voice</c>
/// language name. A key ends up in a path, so the set is an allowlist: anything else is dropped at
/// catalogue load, never carried into a file name.
/// </summary>
public static class VoiceLanguages
{
    /// <summary>The pack every install used to carry unconditionally; the wizard pre-selects it and the
    /// legacy catalogue key <c>audio</c> means exactly this one.</summary>
    public const string Default = "English(US)";

    /// <summary>Display order, and the order the catalogue's packs are reported in.</summary>
    public static readonly string[] All = { "English(US)", "Chinese", "Japanese", "Korean" };

    private static readonly Dictionary<string, string> Slugs = new(StringComparer.Ordinal)
    {
        ["English(US)"] = "en", ["Chinese"] = "zh", ["Japanese"] = "ja", ["Korean"] = "ko",
    };

    /// <summary>The allowlist spelling of <paramref name="lang"/> (matched case-insensitively, trimmed),
    /// or null when it is not a language Relic knows.</summary>
    public static string? Canonical(string? lang)
    {
        if (string.IsNullOrWhiteSpace(lang)) return null;
        string t = lang.Trim();
        return All.FirstOrDefault(l => string.Equals(l, t, StringComparison.OrdinalIgnoreCase));
    }

    public static bool IsKnown(string? lang) => Canonical(lang) is not null;

    /// <summary>Short file-name-safe tag (en/zh/ja/ko) for the download cache. Only ever called with a
    /// canonical name — an unknown language is a bug upstream, not a file to create.</summary>
    public static string Slug(string lang) =>
        Canonical(lang) is { } c ? Slugs[c] : throw new ArgumentException($"unknown voice language '{lang}'", nameof(lang));

    /// <summary>The marker the language zip lays at the game root: the client's own integrity list for
    /// that pack (<c>{"remoteName": "...", "md5": ..., "fileSize": ...}</c> per file). Its presence is what
    /// "installed" means here — a torn extract or a pack the client fetched on its own at login leaves the
    /// sound-bank folder behind WITHOUT it, and neither counts as an installed language.</summary>
    public static string PkgVersionFile(string gameDir, string lang) =>
        Path.Combine(gameDir, $"Audio_{Canonical(lang) ?? lang}_pkg_version");

    /// <summary>Where the pack's <c>.pck</c> files live once extracted.</summary>
    public static string SoundBanksDir(string gameDir, string lang) =>
        Path.Combine(gameDir, "GenshinImpact_Data", "StreamingAssets", "Audio", "GeneratedSoundBanks", "Windows", Canonical(lang) ?? lang);

    /// <summary>Marker-file test only (see <see cref="PkgVersionFile"/>). Never throws: a bad path reads
    /// as "not installed".</summary>
    public static bool IsOnDisk(string gameDir, string lang)
    {
        if (string.IsNullOrWhiteSpace(gameDir) || Canonical(lang) is null) return false;
        try { return File.Exists(PkgVersionFile(gameDir, lang)); }
        catch { return false; }
    }

    /// <summary>The installed languages of a game folder, in <see cref="All"/> order.</summary>
    public static List<string> Detect(string gameDir) => All.Where(l => IsOnDisk(gameDir, l)).ToList();
}

public sealed class GameVersionInfo
{
    public string Id { get; set; } = "";
    public string Title { get; set; } = "";
    public string Desc { get; set; } = "";
    public string Server { get; set; } = "";   // version key the Linux agent uses
    /// <summary>Optional: the in-game login of this version's pre-made save. Normally read straight
    /// from the agent payload's manifest (see <see cref="GameAccounts"/>); set here only as the
    /// fallback for a build that ships without agent/payloads.</summary>
    public string Account { get; set; } = "";
    public GameSourceInfo Client { get; set; } = new();
    /// <summary>The voice packs, keyed by <see cref="VoiceLanguages"/> name (the allowlist spelling —
    /// <see cref="VersionCatalog.Load"/> re-keys case-insensitive input and drops the rest). One
    /// representation for every consumer: size sums, mirror lists, preflight and the install all read
    /// THIS, so the English pack can never fork from the others.</summary>
    public Dictionary<string, GameSourceInfo> Voices { get; set; } = new(StringComparer.OrdinalIgnoreCase);
    /// <summary>LEGACY catalogue key <c>audio</c> = the English(US) pack. Read only while loading —
    /// <see cref="VersionCatalog.Load"/> folds it into <see cref="Voices"/> and nulls it, so code must
    /// read <c>Voices</c>/<see cref="VoicePack"/>, never this. Kept because the private
    /// <c>config/versions.json</c> and the <c>%LOCALAPPDATA%</c> override live outside the repo and may
    /// still be audio-shaped.</summary>
    public GameSourceInfo? Audio { get; set; }

    public GameSourceInfo? VoicePack(string lang) =>
        VoiceLanguages.Canonical(lang) is { } c && Voices.TryGetValue(c, out var p) ? p : null;

    /// <summary>The packs this version carries, in display order.</summary>
    public IEnumerable<(string Lang, GameSourceInfo Pack)> VoicePacks()
    {
        foreach (string l in VoiceLanguages.All)
            if (Voices.TryGetValue(l, out var p)) yield return (l, p);
    }

    /// <summary>Game + the default (English) pack — what the version card has always quoted.</summary>
    public long DefaultInstallSize => Client.Size + (VoicePack(VoiceLanguages.Default)?.Size ?? 0);
}

/// <summary>
/// Loads the installable versions (CDN urls, Drive ids, sizes) from config/versions.json. The real
/// file is gitignored (contains private Drive ids); versions.sample.json is the committed template.
/// </summary>
public static class VersionCatalog
{
    private sealed class Root { public List<GameVersionInfo> Versions { get; set; } = new(); }

    private static readonly JsonSerializerOptions Opts = new() { PropertyNameCaseInsensitive = true };

    public static IReadOnlyList<GameVersionInfo> Load(string? path = null)
    {
        path ??= ResolvePath();
        if (path is null || !File.Exists(path)) return Array.Empty<GameVersionInfo>();
        var root = JsonSerializer.Deserialize<Root>(File.ReadAllText(path), Opts);
        var versions = root?.Versions ?? new List<GameVersionInfo>();
        foreach (var v in versions) Normalize(v);
        return versions;
    }

    /// <summary>
    /// One representation of the voice packs, whatever shape the file had: the legacy <c>audio</c>
    /// object becomes <c>voices["English(US)"]</c> (unless that key is already there — an explicit
    /// entry wins), keys are re-spelt to the allowlist, and anything the install could not use
    /// (an unknown language, a pack with no host at all) is dropped HERE with a log line rather than
    /// hours later as a download error.
    /// </summary>
    private static void Normalize(GameVersionInfo v)
    {
        v.Client ??= new GameSourceInfo();
        v.Client.Mirrors ??= new();
        var raw = v.Voices ?? new Dictionary<string, GameSourceInfo>();
        bool hasDefault = raw.Keys.Any(k => string.Equals(VoiceLanguages.Canonical(k), VoiceLanguages.Default, StringComparison.Ordinal));
        if (v.Audio is not null && v.Audio.HasAnySource && !hasDefault)
            raw[VoiceLanguages.Default] = v.Audio;
        v.Audio = null;

        // The deserializer built `raw` with the default comparer and the file's own spelling; the
        // dictionary handed out is rebuilt case-insensitive AND canonical, so `Voices["english(us)"]`
        // and `Voices["English(US)"]` are one entry — a key reaches a file name downstream.
        var voices = new Dictionary<string, GameSourceInfo>(StringComparer.OrdinalIgnoreCase);
        foreach (var (key, pack) in raw)
        {
            string? canonical = VoiceLanguages.Canonical(key);
            if (canonical is null)
            {
                Log.Info($"catalogue {v.Id}: voice language '{key}' is not supported — ignored");
                continue;
            }
            if (pack is null || !pack.HasAnySource)
            {
                Log.Info($"catalogue {v.Id}: the {canonical} voice pack has no download source — ignored");
                continue;
            }
            if (voices.ContainsKey(canonical))
            {
                Log.Info($"catalogue {v.Id}: the {canonical} voice pack is listed twice ('{key}') — the first entry is kept");
                continue;
            }
            pack.Mirrors ??= new();
            voices[canonical] = pack;
        }
        if (!voices.ContainsKey(VoiceLanguages.Default))
            Log.Info($"catalogue {v.Id}: no {VoiceLanguages.Default} voice pack — the wizard pre-selects the first available language");
        v.Voices = voices;
    }

    /// <summary>Search order: user override, app content dir, then the committed sample.</summary>
    private static string? ResolvePath()
    {
        var candidates = new[]
        {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Relic", "config", "versions.json"),
            Path.Combine(AppContext.BaseDirectory, "config", "versions.json"),
            Path.Combine(AppContext.BaseDirectory, "config", "versions.sample.json"),
        };
        return candidates.FirstOrDefault(File.Exists);
    }
}

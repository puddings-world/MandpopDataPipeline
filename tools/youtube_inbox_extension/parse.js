// Heuristics: YouTube video title + channel → song fields. Every field stays
// editable in the panel, so this only has to be right most of the time.
(function (root) {
  const CN = { 一: 1, 二: 2, 兩: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9, 十: 10 };
  const N = "([0-9]{1,2}|[一二兩三四五六七八九十])";
  const toNum = (s) => (/^\d+$/.test(s) ? +s : CN[s] || 0);

  const NOISE = /(official|mv|m\/v|music video|lyrics?|lyric video|audio|karaoke|instrumental|ktv|伴奏|伴唱|純音樂|消音|去人聲|動態歌詞|歌詞|高音質|完整版|官方|cover|翻唱|live|hd|4k|key|調)/i;

  // Key the VIDEO itself is in, relative to the original song.
  function detectVideoKey(t) {
    let m = t.match(new RegExp(`(降|低|升|高)\\s*${N}\\s*個?\\s*(key|調|度|半音|個key)`, "i"));
    if (m) return (m[1] === "降" || m[1] === "低" ? -1 : 1) * toNum(m[2]);
    m = t.match(/([+\-−＋－])\s*(\d{1,2})\s*key/i) || t.match(/key\s*([+\-−＋－])\s*(\d{1,2})/i);
    if (m) return ("+＋".includes(m[1]) ? 1 : -1) * +m[2];
    return 0;
  }

  function detectGender(t) {
    if (/女\s*(key|調|生版|版)/i.test(t)) return "female";
    if (/男\s*(key|調|生版|版)/i.test(t)) return "male";
    return null;
  }

  // "告白氣球 Love Confession" → "告白氣球"; leave pure-latin titles alone.
  function dropLatinTail(s) {
    if (!/[㐀-鿿]/.test(s)) return s.trim();
    return s.replace(/\s+[A-Za-z][A-Za-z0-9 .'&’,!?-]*$/, "").trim();
  }

  function stripNoise(s) {
    return s
      .replace(/[(（\[［][^)）\]］]*[)）\]］]/g, (g) => (NOISE.test(g) ? " " : g))
      .replace(/[|｜].*$/, " ")
      .split(/\s+/)
      .filter((w) => w && !NOISE.test(w) && !/^[降低升高+\-−]/.test(w))
      .join(" ")
      .trim();
  }

  function parseVideo(rawTitle, channel) {
    const t = (rawTitle || "").replace(/\s+-\s+YouTube$/, "").trim();
    const ch = (channel || "").trim();
    const out = { title: "", artist: "", videoKey: detectVideoKey(t), gender: detectGender(t) };

    if (/ - Topic$/.test(ch)) {                       // auto-generated "Artist - Topic"
      out.artist = ch.replace(/ - Topic$/, "");
      out.title = dropLatinTail(stripNoise(t) || t);
      return out;
    }

    const br = t.match(/[【《「『]([^】》」』]+)[】》」』]/);
    if (br) {
      out.title = dropLatinTail(stripNoise(br[1]) || br[1]);
      out.artist = dropLatinTail(stripNoise(t.replace(br[0], " ")));
    } else {
      const parts = t.split(/\s+[-–—]\s+/);
      if (parts.length >= 2) {
        let [a, b] = [stripNoise(parts[0]), stripNoise(parts.slice(1).join(" "))];
        const chCore = dropLatinTail(ch.replace(/(official|官方|channel|頻道)/gi, "")).trim();
        if (chCore && a.includes(chCore) === false && b.includes(chCore)) [a, b] = [b, a];
        out.artist = dropLatinTail(a);
        out.title = dropLatinTail(b);
      } else {
        out.title = dropLatinTail(stripNoise(t));
      }
    }
    if (!out.artist) out.artist = dropLatinTail(ch.replace(/(official|官方|channel|頻道)/gi, "")).trim();
    if (out.artist && out.title.startsWith(out.artist + " ")) out.title = out.title.slice(out.artist.length + 1);
    out.title = out.title.replace(/[\/／#＃!！]/g, " ").replace(/\s+/g, " ").trim();
    out.artist = out.artist.replace(/[\/／#＃]/g, " ").replace(/\s+/g, " ").trim();
    return out;
  }

  root.SongParse = { parseVideo, detectVideoKey, detectGender };
  if (typeof module !== "undefined") module.exports = root.SongParse;
})(typeof globalThis !== "undefined" ? globalThis : this);

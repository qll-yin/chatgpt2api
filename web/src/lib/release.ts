export type ReleaseInfo = {
  version: string;
  date: string;
  items: { type: string; content: string }[];
};

export function parseChangelog(content: string): ReleaseInfo[] {
  return content
    .split(/^## /m)
    .slice(1)
    .map((block) => {
      // ⚠️ title 必须单独 trim 一次。
      //    split("\n") 在 CRLF 文件里会留下行尾的 "\r"（"1.11.0 - 2026-11-14\r"），
      //    而正则里的 `.` 不匹配行终止符 → 整体失配 → version 变成整行标题、
      //    date 恒为空 → 前端「最新」「当前」两个徽章永远匹配不上
      //    （它们比的是纯版本号，如 "1.11.0"）。
      const title = (block.trim().split("\n")[0] || "").trim();
      const [, ...lines] = block.trim().split("\n");
      // 标题形如 `1.11.0 - 2026-11-14`：版本号和日期要拆开。
      // 拆不出来（比如 "Unreleased"）就把整串当版本号。
      const match = title.match(/^(.+?)\s+-\s+(.+)$/);
      return {
        version: match ? match[1].trim() : title,
        date: match ? match[2].trim() : "",
        items: lines
          .map((line) => line.trim().match(/^\+\s+\[(.+?)\]\s+(.+)$/))
          .filter((match): match is RegExpMatchArray => Boolean(match))
          .map((match) => ({ type: match[1], content: match[2] })),
      };
    })
    .filter((release) => release.items.length);
}

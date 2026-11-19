/** Open a link in the user's default browser. Best effort: callers always show the link too. */
export async function openInBrowser(url: string): Promise<void> {
  const command =
    process.platform === "darwin"
      ? ["open", url]
      : process.platform === "win32"
        ? ["cmd", "/c", "start", "", url]
        : ["xdg-open", url];
  const child = Bun.spawn(command, { stdin: "ignore", stdout: "ignore", stderr: "ignore" });
  const code = await child.exited;
  if (code !== 0) throw new Error(`${command[0]} exited with ${code}`);
}

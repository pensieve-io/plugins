import { deleteCredentials, loadCredentials } from "../lib/credentials.js";

export default {
  name: "logout",
  summary: "Delete the CLI's stored Pensieve credentials",
  usage: `Usage: pensieve logout

Deletes ~/.pensieve/credentials.json. The plugin's sign-in inside Claude Code
or Codex is separate and is not affected.`,
  options: {},

  async run(io) {
    const existed = !!(await loadCredentials(io));
    await deleteCredentials(io);
    io.stdout.write(existed ? "Signed out of the Pensieve CLI.\n" : "Not signed in.\n");
    return 0;
  },
};

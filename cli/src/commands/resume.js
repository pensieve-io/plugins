import { resume } from "../lib/pause.js";

export default {
  name: "resume",
  summary: "Go back on the record: let the plugin save transcripts again",
  usage: `Usage: pensieve resume

Deletes the plugin's pause file. Transcript saving and commit/PR anchors start
again from new activity, where you have approved sharing; nothing from the
paused period is uploaded.`,
  options: {},

  async run(io) {
    const resumed = await resume(io);
    io.stdout.write(
      resumed
        ? "Resumed: the Pensieve plugin saves transcripts and records commit/PR anchors again from new activity; nothing from the paused period will be uploaded. Briefings and MCP tools were unaffected.\n"
        : "Not paused; nothing to resume. The Pensieve plugin is already saving transcripts and commit/PR anchors where you have approved sharing.\n",
    );
    return 0;
  },
};

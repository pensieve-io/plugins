import { PAUSE_EFFECT, pause } from "../lib/pause.js";

export default {
  name: "pause",
  summary: "Go off the record: stop the plugin saving transcripts on this machine",
  usage: `Usage: pensieve pause

Writes \`paused.json\` to the plugin's capture state directory
(PENSIEVE_CAPTURE_STATE, else ~/.local/state/pensieve/capture).

${PAUSE_EFFECT}

Run \`pensieve resume\` to start saving again. Takes effect with plugin
versions whose hooks read the pause file.`,
  options: {},

  async run(io) {
    const result = await pause(io);
    const lead = !result.written
      ? `Already paused since ${result.since}.`
      : result.replaced
        ? `Paused since ${result.since} (replaced an unreadable pause file).`
        : `Paused since ${result.since}.`;
    io.stdout.write(`${lead} ${PAUSE_EFFECT}\n`);
    return 0;
  },
};

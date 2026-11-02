// Answers on the Nightscout port until the first build finishes. Without it the
// Supervisor watchdog sees a dead app and restarts it, killing the build.
require('http')
  .createServer((req, res) => {
    res.setHeader('Content-Type', 'text/plain');
    res.end('Nightscout is being installed. See the app log for progress.\n');
  })
  .listen(1337);

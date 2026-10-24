// RailRush Phase 8: one full purchase journey per virtual user, run by k6 inside the compose network.
// join the waiting room -> poll until admitted -> hold seats (retry on conflict) -> book -> pay -> signed webhook.
import http from "k6/http";
import crypto from "k6/crypto";
import exec from "k6/execution";
import { check, sleep } from "k6";
import { SharedArray } from "k6/data";
import { Counter, Trend } from "k6/metrics";

const BASE = __ENV.BASE_URL || "http://nginx:8080";
const USERS = parseInt(__ENV.USERS || "100", 10);
const RAMP = parseFloat(__ENV.RAMP_SECONDS || "20");
const POLL = parseFloat(__ENV.POLL_SECONDS || "2");
const MAX_WAIT = parseFloat(__ENV.MAX_WAIT_SECONDS || "300");
const MAX_SEATS = parseInt(__ENV.SEATS_PER_USER || "2", 10);
const ABANDON = parseFloat(__ENV.ABANDON_RATE || "0.1");
const FAIL_PAY = parseFloat(__ENV.FAIL_PAY_RATE || "0.05");
const IP_POOL = parseInt(__ENV.IP_POOL || "200", 10);
const SECRET = __ENV.WEBHOOK_SECRET || "";
const GATEWAY_IPS = ["198.51.100.10", "198.51.100.11"];
// "sleep" = the intended client contract: sleep until the ticket's admit_at, then ask for the pass.
// "poll" = ask again every POLL_SECONDS (what a badly written client would do).
const MODE = __ENV.QUEUE_MODE || "sleep";
// "full" = the whole purchase journey. "join" = only take a place in the queue (the opening burst).
const JOURNEY = __ENV.JOURNEY || "full";

const tokens = new SharedArray("tokens", () => JSON.parse(open("/data/tokens.json")));
const runs = new SharedArray("runs", () => JSON.parse(open("/data/runs.json")));

// 409 (conflicts) and 429 (throttling) are expected answers in a rush; everything else non-2xx is a failure.
http.setResponseCallback(http.expectedStatuses({ min: 200, max: 299 }, 409, 429));

const started = new Counter("journeys_started");
const confirmed = new Counter("journeys_confirmed");
const soldOut = new Counter("journeys_sold_out");
const gaveUpInQueue = new Counter("journeys_gave_up_in_queue");
const abandonedPayment = new Counter("journeys_abandoned_payment");
const failedPayment = new Counter("journeys_payment_failed");
const bookFailed = new Counter("journeys_booking_failed");
const holdConflicts = new Counter("hold_conflicts");
const throttled = new Counter("throttled_429");
const webhookRetries = new Counter("webhook_retries");
const webhookGaveUp = new Counter("webhook_gave_up");
const queueWait = new Trend("queue_wait_seconds");
const journeySeconds = new Trend("journey_seconds");

export const options = {
  scenarios: {
    rush: { executor: "per-vu-iterations", vus: USERS, iterations: 1, maxDuration: __ENV.MAX_DURATION || "20m" },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{name:me}": ["p(95)<500"],
    "http_req_duration{name:hold}": ["p(95)<800"],
    "http_req_duration{name:book}": ["p(95)<1000"],
    "http_req_duration{name:webhook}": ["p(95)<500"],
    "http_req_duration{name:join}": ["p(95)>=0"],
    "http_req_duration{name:pay}": ["p(95)>=0"],
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
};

function clientIp(uid) {
  return "203.0.113." + ((uid % IP_POOL) + 1);
}

function headers(token, uid, extra) {
  return Object.assign({ Authorization: "Bearer " + token, "X-Forwarded-For": clientIp(uid) }, extra || {});
}

function retryAfter(res) {
  const v = parseFloat(res.headers["Retry-After"] || res.headers["retry-after"]);
  return isFinite(v) && v > 0 ? Math.min(v, 10) : 1;
}

function json(res) {
  try {
    return res.json();
  } catch (e) {
    return {};
  }
}

// Honour Retry-After on 429, as a real client should. The booking's Idempotency-Key makes the retry safe.
function postRetry(url, body, params) {
  let res = http.post(url, body, params);
  for (let attempt = 0; attempt < 4 && res.status === 429; attempt++) {
    throttled.add(1);
    sleep(retryAfter(res));
    res = http.post(url, body, params);
  }
  return res;
}

export function setup() {
  if (tokens.length < USERS) {
    throw new Error("tokens.json has " + tokens.length + " tokens but USERS=" + USERS);
  }
  const seatsByRun = {};
  for (let i = 0; i < runs.length; i++) {
    const runId = runs[i];
    const res = http.get(BASE + "/api/catalog/runs/" + runId + "/seats/", { tags: { name: "seatmap" } });
    if (res.status !== 200) {
      throw new Error("seat map for run " + runId + " returned " + res.status);
    }
    const labels = [];
    const coaches = res.json("coaches");
    for (let c = 0; c < coaches.length; c++) {
      for (let row = 1; row <= coaches[c].rows; row++) {
        for (const col of coaches[c].columns) {
          labels.push(coaches[c].number + "-" + row + col);
        }
      }
    }
    seatsByRun[runId] = labels;
  }
  return { seatsByRun: seatsByRun };
}

export default function (data) {
  const uid = exec.vu.idInTest - 1;
  const token = tokens[uid];
  const runId = runs[uid % runs.length];
  const seats = data.seatsByRun[runId];
  started.add(1);
  sleep(Math.random() * RAMP);
  const t0 = Date.now();

  // 1. join the waiting room
  let r = postRetry(BASE + "/api/waitroom/" + runId + "/join/", null, { headers: headers(token, uid), tags: { name: "join" } });
  if (r.status === 429) {
    throttled.add(1);
    sleep(retryAfter(r));
    r = http.post(BASE + "/api/waitroom/" + runId + "/join/", null, { headers: headers(token, uid), tags: { name: "join" } });
  }
  if (!check(r, { "join accepted": (x) => x.status === 200 || x.status === 201 })) {
    gaveUpInQueue.add(1);
    return;
  }
  if (JOURNEY === "join") {
    return;
  }

  // 2. wait in the queue until admitted
  const queueStart = Date.now();
  let pass = null;
  if (MODE === "sleep") {
    // The join answer already says when this ticket is admitted, so sleep until then instead of polling.
    const ticket = json(r);
    if (ticket.state === "waiting" && ticket.wait_seconds > 0) {
      sleep(Math.min(ticket.wait_seconds, MAX_WAIT) + 0.05 + Math.random() * 0.2);
    }
  }
  while ((Date.now() - queueStart) / 1000 < MAX_WAIT) {
    const m = http.get(BASE + "/api/waitroom/" + runId + "/me/", { headers: headers(token, uid), tags: { name: "me" } });
    if (m.status === 200) {
      const body = json(m);
      if (body.state === "admitted") {
        pass = body.waitroom_pass;
        break;
      }
      if (MODE === "sleep" && body.state === "waiting" && body.wait_seconds > 0) {
        sleep(Math.min(body.wait_seconds, 60) + 0.05 + Math.random() * 0.2);
        continue;
      }
    } else if (m.status === 429) {
      throttled.add(1);
      sleep(retryAfter(m));
      continue;
    }
    sleep(POLL + Math.random());
  }
  queueWait.add((Date.now() - queueStart) / 1000);
  if (!pass) {
    gaveUpInQueue.add(1);
    return;
  }

  // 3. hold seats, trying different seats when someone else got there first
  let held = false;
  for (let attempt = 0; attempt < 4 && !held; attempt++) {
    const want = 1 + Math.floor(Math.random() * MAX_SEATS);
    const pick = new Set();
    while (pick.size < want) {
      pick.add(seats[Math.floor(Math.random() * seats.length)]);
    }
    const h = http.post(BASE + "/api/holds/" + runId + "/", JSON.stringify({ seats: Array.from(pick) }), {
      headers: headers(token, uid, { "Content-Type": "application/json", "X-Waitroom-Pass": pass }),
      tags: { name: "hold" },
    });
    if (h.status === 200 || h.status === 201) {
      held = true;
    } else if (h.status === 409) {
      holdConflicts.add(1);
      sleep(0.3 + Math.random());
    } else if (h.status === 429) {
      throttled.add(1);
      sleep(retryAfter(h));
    } else {
      break;
    }
  }
  if (!held) {
    soldOut.add(1);
    return;
  }
  sleep(1 + Math.random() * 2); // the buyer checks the details

  // 4. book the held seats
  const jsonHeaders = { "Content-Type": "application/json" };
  const b = postRetry(BASE + "/api/bookings/", JSON.stringify({ run_id: runId }), {
    headers: headers(token, uid, Object.assign({ "Idempotency-Key": "lt-journey-" + uid + "-" + runId }, jsonHeaders)),
    tags: { name: "book" },
  });
  if (b.status !== 200 && b.status !== 201) {
    if (b.status === 429) {
      throttled.add(1);
    }
    bookFailed.add(1);
    return;
  }
  const booking = json(b);

  // 5. open a payment
  const p = postRetry(BASE + "/api/bookings/" + booking.id + "/pay/", "{}", {
    headers: headers(token, uid, jsonHeaders),
    tags: { name: "pay" },
  });
  if (p.status !== 200 && p.status !== 201) {
    bookFailed.add(1);
    return;
  }
  const payment = json(p).payment;
  if (Math.random() < ABANDON) {
    abandonedPayment.add(1); // never pays: the booking has to expire by itself
    return;
  }

  // 6. the gateway calls back (from two fixed gateway addresses), and retries when refused
  sleep(1 + Math.random() * 3);
  const succeeded = Math.random() >= FAIL_PAY;
  const payload = JSON.stringify({
    provider_ref: payment.provider_ref,
    status: succeeded ? "succeeded" : "failed",
    amount_pkr: payment.amount_pkr,
  });
  const signature = crypto.hmac("sha256", SECRET, payload, "hex");
  let delivered = false;
  for (let attempt = 0; attempt < 6 && !delivered; attempt++) {
    const w = http.post(BASE + "/api/payments/webhook/", payload, {
      headers: {
        "Content-Type": "application/json",
        "X-Gateway-Signature": signature,
        "X-Forwarded-For": GATEWAY_IPS[uid % GATEWAY_IPS.length],
      },
      tags: { name: "webhook" },
    });
    if (w.status === 200) {
      delivered = true;
    } else {
      webhookRetries.add(1);
      if (w.status === 429) {
        throttled.add(1);
      }
      sleep(w.status === 429 ? retryAfter(w) : Math.pow(2, attempt));
    }
  }
  if (!delivered) {
    webhookGaveUp.add(1);
    return;
  }
  if (!succeeded) {
    failedPayment.add(1);
    return;
  }

  // 7. the buyer sees the confirmed booking
  const g = http.get(BASE + "/api/bookings/" + booking.id + "/", { headers: headers(token, uid), tags: { name: "get_booking" } });
  if (json(g).status === "confirmed") {
    confirmed.add(1);
    journeySeconds.add((Date.now() - t0) / 1000);
  }
}

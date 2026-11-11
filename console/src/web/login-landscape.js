(() => {
  const page = document.documentElement;
  const canvas = document.querySelector('#landscape');
  const hasLoginError = Boolean(document.querySelector('#login-status')?.textContent.trim());
  const preference = matchMedia('(prefers-reduced-motion: reduce)');

  const context = canvas.getContext('2d', { alpha: false });
  if (!context) return;

  const clamp = (value) => Math.max(0, Math.min(1, value));
  const mix = (a, b, t) => a + (b - a) * t;
  function smooth(start, end, value) {
    const t = clamp((value - start) / (end - start));
    return t * t * (3 - 2 * t);
  }
  function rgb(hex) {
    const n = Number.parseInt(hex, 16);
    return [n >> 16, n >> 8 & 255, n & 255];
  }
  function sheet(width, height) {
    const element = document.createElement('canvas');
    element.width = width;
    element.height = height;
    return element.getContext('2d');
  }

  let seed = 43957;
  function random() {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 4294967296;
  }

  // The paint tile measures colour and grain down the slip face, starting at the crest.
  const paintWidth = 128;
  const paintHeight = 320;
  function sandPaint(stops) {
    const brush = sheet(paintWidth, paintHeight);
    const image = brush.createImageData(paintWidth, paintHeight);
    const colors = stops.map(([position, hex]) => [position, rgb(hex)]);
    let band = 0;
    for (let y = 0; y < paintHeight; y++) {
      const depth = y / (paintHeight - 1);
      if (band < colors.length - 2 && depth > colors[band + 1][0]) band++;
      const [from, first] = colors[band];
      const [to, second] = colors[band + 1];
      const blend = smooth(from, to, depth);
      const color = first.map((channel, index) => mix(channel, second[index], blend));
      for (let x = 0; x < paintWidth; x++) {
        const offset = (y * paintWidth + x) * 4;
        const grain = (random() + random() - 1) * 16;
        const fleck = random() > .989 ? 13 : 0;
        for (let channel = 0; channel < 3; channel++) image.data[offset + channel] = color[channel] + grain + fleck;
        image.data[offset + 3] = 255;
      }
    }
    brush.putImageData(image, 0, 0);
    return brush.canvas;
  }

  const ridges = [
    {
      crest: [.462, .505, .527, .551, .534, .555, .601, .583, .59, .61, .598],
      gust: [.537, .575, .551, .556, .596, .593, .563, .542, .56, .558, .516],
      colors: [[0, '8b806c'], [.025, '918570'], [.056, 'c3b497'], [.15, 'b1a186'], [.37, '8b7c64'], [.65, '4d4235'], [1, '332b22']]
    },
    {
      crest: [.788, .765, .725, .699, .704, .689, .633, .62, .612, .623, .601],
      gust: [.71, .649, .638, .655, .63, .593, .572, .598, .586, .6, .549],
      colors: [[0, '857866'], [.034, '958873'], [.075, 'b6a588'], [.28, 'a28f74'], [.61, '685844'], [1, '2c241c']]
    },
    {
      crest: [.77, .737, .709, .758, .757, .745, .815, .869, .897, .883, .908],
      gust: [.881, .832, .86, .822, .794, .741, .775, .765, .786, .747, .805],
      colors: [[0, '887e6b'], [.055, '8f826c'], [.114, 'b6a78a'], [.29, 'a39479'], [.56, '6f604c'], [1, '29221a']]
    },
    {
      crest: [.937, .924, .971, .953, .967, .981, 1.006, .97, .911, .906, .936],
      gust: [.99, .938, .977, .911, .821, .994, 1.009, .93, .911, .968, .978],
      colors: [[0, '53493a'], [.12, '564b3b'], [.32, '483e30'], [.63, '322a21'], [1, '241d17']]
    }
  ].map(({ crest, gust, colors }) => ({ crest, gust, paint: sandPaint(colors) }));

  function crestAt(points, position) {
    const scaled = position * (points.length - 1);
    const index = Math.min(points.length - 2, Math.floor(scaled));
    const t = scaled - index;
    const p0 = points[Math.max(0, index - 1)];
    const p1 = points[index];
    const p2 = points[index + 1];
    const p3 = points[Math.min(points.length - 1, index + 2)];
    return p1 + .5 * t * ((p2 - p0) + t * (2 * p0 - 5 * p1 + 4 * p2 - p3 + t * (3 * (p1 - p2) + p3 - p0)));
  }

  // Bake the turbulence once. The moving cloud layers sample the same dust at different scales.
  const noiseSize = 64;
  const noiseGrid = Float32Array.from({ length: noiseSize ** 2 }, random);
  function noise(x, y) {
    const i = Math.floor(x);
    const j = Math.floor(y);
    const u = smooth(0, 1, x - i);
    const v = smooth(0, 1, y - j);
    const first = (j & 63) * noiseSize;
    const second = ((j + 1) & 63) * noiseSize;
    return mix(
      mix(noiseGrid[first + (i & 63)], noiseGrid[first + ((i + 1) & 63)], u),
      mix(noiseGrid[second + (i & 63)], noiseGrid[second + ((i + 1) & 63)], u), v
    );
  }

  function billow(offset, dark, light) {
    const width = 320;
    const height = 144;
    const brush = sheet(width, height);
    const image = brush.createImageData(width, height);
    const shadows = rgb(dark);
    const highlights = rgb(light);
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const u = x / (width - 1);
        const v = y / (height - 1);
        const body = Math.sin(Math.PI * u) ** .75;
        const high = noise(u * 6 + offset, v * 4 + 8);
        const fine = noise(u * 17 + 3, v * 13 + offset);
        const detail = noise(u * 43 + offset, v * 27 + 12);
        const coil = .68 - .31 * Math.sin(u * Math.PI * .84) + (noise(u * 5 + offset, 15) - .5) * .31;
        const lift = (v - coil) / (.11 + .34 * body);
        const density = 1 - Math.abs(lift) + (high - .5) * .94 + (fine - .5) * .48 + (detail - .5) * .17;
        const edge = smooth(0, .11, u) * (1 - smooth(.8, 1, u)) * smooth(0, .12, v) * (1 - smooth(.8, 1, v));
        const alpha = smooth(.03, .82, density) * edge;
        const glow = clamp(.56 - lift * .48 + (fine - .5) * .34 - (high - .5) * .26);
        const grit = (random() - .5) * 13;
        const index = (y * width + x) * 4;
        for (let channel = 0; channel < 3; channel++) image.data[index + channel] = mix(shadows[channel], highlights[channel], glow) + grit;
        image.data[index + 3] = 255 * alpha;
      }
    }
    brush.putImageData(image, 0, 0);
    return brush.canvas;
  }

  const farDust = billow(6, '97836b', 'f2e2be');
  const rollingDust = billow(19, '5b4935', 'e6c88d');
  const nearDust = billow(33, '483a2b', 'c6aa77');
  const grit = Array.from({ length: 440 }, () => ({
    travel: random(), height: random(), speed: .6 + random() * .8,
    radius: random() ** 2 * 1.6 + .35, phase: random() * 6.28, light: random() > .43
  }));

  const dustGrain = sheet(160, 160);
  const dots = dustGrain.createImageData(160, 160);
  for (let i = 0; i < dots.data.length; i += 4) {
    const value = random() > .51 ? 251 : 51;
    dots.data[i] = value;
    dots.data[i + 1] = value;
    dots.data[i + 2] = value;
    dots.data[i + 3] = random() * 17;
  }
  dustGrain.putImageData(dots, 0, 0);
  const grainPattern = context.createPattern(dustGrain.canvas, 'repeat');

  const spark = sheet(48, 12);
  spark.save();
  spark.scale(4, 1);
  const sparkGlow = spark.createRadialGradient(6, 6, 0, 6, 6, 6);
  sparkGlow.addColorStop(0, 'rgba(255, 237, 194, .98)');
  sparkGlow.addColorStop(.35, 'rgba(246, 220, 163, .63)');
  sparkGlow.addColorStop(1, 'rgba(231, 194, 130, 0)');
  spark.fillStyle = sparkGlow;
  spark.fillRect(0, 0, 12, 12);
  spark.restore();

  let width, height, density, mobile, sky, skyLight, baseHaze;
  let introStart = null;
  let riseStart = null;
  let animationFrame = 0;
  let lastDraw = 0;
  let phase = 0;
  const windEnd = 2.74;
  const introSpeed = 1.45;
  const riseDuration = .54;
  const layerDelay = .11;
  const jointDelay = .045;
  const riseEnd = (ridges.length - 1) * layerDelay + jointDelay + riseDuration;

  function resize() {
    const cssWidth = canvas.clientWidth;
    const cssHeight = canvas.clientHeight;
    if (!cssWidth || !cssHeight) return;
    mobile = cssWidth <= 920;
    density = Math.min(window.devicePixelRatio || 1, 1.28, Math.sqrt(1400000 / (cssWidth * cssHeight)));
    width = canvas.width = Math.round(cssWidth * density);
    height = canvas.height = Math.round(cssHeight * density);

    sky = context.createLinearGradient(0, 0, 0, height);
    sky.addColorStop(0, '#f3f0e9');
    sky.addColorStop(.53, '#d9d1c2');
    sky.addColorStop(1, '#70604a');
    skyLight = context.createRadialGradient(width * .14, height * .08, 0, width * .14, height * .08, Math.max(width * .7, height * .46));
    skyLight.addColorStop(0, 'rgba(253, 250, 242, .68)');
    skyLight.addColorStop(1, 'rgba(253, 250, 242, 0)');
    baseHaze = context.createLinearGradient(0, height * .31, 0, height);
    baseHaze.addColorStop(0, 'rgba(110, 90, 62, 0)');
    baseHaze.addColorStop(1, 'rgba(62, 48, 32, .42)');
    render(performance.now());
  }

  function frontAt(y, center, time) {
    return center + width * (.26 * (y - .58) + .083 * Math.sin(y * 7.5 - 1.4) + .024 * Math.sin(y * 14.4 - time * 2.3));
  }

  function riseAmount(time) {
    const remaining = clamp(time / riseDuration) - 1;
    return 1 + remaining ** 2 * (2.25 * remaining + 1.25);
  }

  function drawTerrain(riseTime) {
    const step = width > 1100 ? 8 : 4;
    for (let index = 0; index < ridges.length; index++) {
      const ridge = ridges[index];
      const layerTime = riseTime - index * layerDelay;
      // Neighbouring control points respond at different times. The spline carries
      // the change along each connected ridge while the faces stretch with it.
      const points = ridge.crest.map((rest, joint) => {
        const x = joint / (ridge.crest.length - 1);
        const angle = phase * (.76 + index * .051) - x * 5.7 - index * .94;
        const pose = .455 * (1 - Math.cos(angle));
        const y = mix(rest, ridge.gust[joint], pose) + Math.sin(angle * 1.3 + index) * (.006 + index * .002);
        const crest = y + (mobile ? .065 * (1 - y) : 0);
        const tucked = 1.02 + .06 * (crest - .75);
        return mix(tucked, crest, riseAmount(layerTime - x * jointDelay));
      });
      if (points.every((y) => y >= 1)) continue;
      const profile = (x) => height * crestAt(points, x);

      const outline = new Path2D();
      const samples = [];
      for (let x = 0; x < width; x += step) {
        const y = profile(x / width);
        samples.push(y);
        if (x === 0) outline.moveTo(x, y);
        else outline.lineTo(x, y);
      }
      const end = profile(1);
      outline.lineTo(width, end);
      samples.push(end);

      const slope = new Path2D(outline);
      slope.lineTo(width, height);
      slope.lineTo(0, height);
      slope.closePath();
      context.save();
      context.clip(slope);
      for (let x = 0, point = 0; x < width; x += step, point++) {
        const span = Math.min(step, width - x);
        const top = Math.min(samples[point], samples[point + 1]) - 1.1;
        if (top >= height) continue;
        context.drawImage(ridge.paint, x % paintWidth, 0, span, paintHeight, x, top, span, height - top + 1);
      }

      context.lineWidth = density * (index === 3 ? 1.5 : 2);
      context.strokeStyle = index === 0 ? 'rgba(237, 207, 145, .52)' : 'rgba(227, 195, 132, .42)';
      context.stroke(outline);
      const glow = smooth(.025, .14, layerTime) * (1 - smooth(.26, riseDuration, layerTime));
      if (glow > .025) {
        const light = width * mix(.07, .91, smooth(0, riseDuration, layerTime));
        const gleam = context.createLinearGradient(light - width * .31, 0, light + width * .19, 0);
        gleam.addColorStop(0, 'rgba(255, 218, 145, 0)');
        gleam.addColorStop(.64, `rgba(255, 230, 170, ${glow * .72})`);
        gleam.addColorStop(1, 'rgba(255, 218, 145, 0)');
        context.strokeStyle = gleam;
        context.lineWidth = density * 4;
        context.stroke(outline);
      }
      context.restore();
    }
  }

  function cloud(texture, x, y, w, h, turn, opacity) {
    context.save();
    context.translate(x * width, y * height);
    context.rotate(turn);
    context.globalAlpha = clamp(opacity);
    context.drawImage(texture, -width * w * .5, -height * h * .5, width * w, height * h);
    context.restore();
  }

  function wind(time, center, energy) {
    const position = center / width;
    const wake = smooth(0, .28, time) * (1 - smooth(1.18, 2.05, time));
    const lift = smooth(.36, 1.53, time);
    const curl = .022 * Math.sin(time * 4.9);

    // A low gust enters first, with a soft far bank and darker sand closer to the camera.
    cloud(farDust, .03 + time * .55, .56 - lift * .16, 1.18, .70, -.1, wake * .56);
    cloud(rollingDust, -.12 + time * .7, .74 - lift * .20 + curl, 1.07, .86, -.16, wake * .98);
    cloud(nearDust, -.27 + time * .66, .96 - lift * .16, 1.43, .94, -.075, wake * .86);
    cloud(farDust, position - .17, .54, 1.22, 1.12, -.22, energy * .59);
    cloud(rollingDust, position - .095, .74 - lift * .14, .96, .96, -.21 + curl, energy * .88);
    cloud(farDust, position - .28, .74, .88, .49, -.09, energy * .47);
    cloud(nearDust, position - .26, .98, 1.36, .92, -.12, energy * .72);

    const count = mobile ? 238 : grit.length;
    for (let index = 0; index < count; index++) {
      const grain = grit[index];
      const travel = (grain.travel + time * grain.speed * .71) % 1;
      const y = .47 + grain.height ** .77 * .57 - lift * .12 + Math.sin(travel * 7.3 + grain.phase) * .045;
      const leading = mix(.27 + time * .6, frontAt(y, center, time) / width, smooth(.48, 1.2, time));
      const x = leading - .63 + travel * .86;
      const envelope = smooth(0, .16, travel) * (1 - smooth(.8, 1, travel));
      const size = grain.radius * density;
      context.globalAlpha = (energy * .62 + wake * .38) * envelope;
      if (grain.light) {
        context.drawImage(spark.canvas, x * width, y * height, size * (2.6 + energy * 5), size * 1.5);
      } else {
        context.fillStyle = '#8e7655';
        context.fillRect(x * width, y * height, size * 1.4, size * .66);
      }
    }
    context.globalAlpha = 1;
  }

  function introTime(now) {
    return Math.max(0, (now - introStart) * introSpeed / 1000);
  }

  function render(now) {
    if (!width || !height) return;

    context.fillStyle = sky;
    context.fillRect(0, 0, width, height);
    context.fillStyle = skyLight;
    context.fillRect(0, 0, width, height);
    context.fillStyle = baseHaze;
    context.fillRect(0, 0, width, height);

    if (introStart !== null) {
      const time = introTime(now);
      const front = mix(.025, 1.4, smooth(.52, 2.18, time)) * width;
      const energy = smooth(.46, 1.03, time) * (1 - smooth(1.68, windEnd, time));
      wind(time, front, energy);
    } else {
      drawTerrain(riseStart === null ? riseEnd : (now - riseStart) / 1000);
    }

    context.fillStyle = grainPattern;
    context.fillRect(0, 0, width, height);
  }

  function queue() {
    if (!animationFrame && !preference.matches) animationFrame = requestAnimationFrame(frame);
  }

  function frame(now) {
    animationFrame = 0;
    if (now - lastDraw >= 26) {
      phase += Math.min(.065, (now - lastDraw) / 1000);
      lastDraw = now;
      if (introStart !== null && introTime(now) >= windEnd) {
        introStart = null;
        riseStart = now;
      } else if (riseStart !== null && now - riseStart >= riseEnd * 1000) {
        riseStart = null;
        page.classList.replace('intro', 'entered');
      }
      render(now);
    }
    queue();
  }

  function applyMotionPreference() {
    if (preference.matches) {
      introStart = null;
      riseStart = null;
      page.classList.remove('intro', 'entered');
      cancelAnimationFrame(animationFrame);
      animationFrame = 0;
      render(performance.now());
    } else {
      lastDraw = performance.now();
      queue();
    }
  }

  preference.addEventListener('change', applyMotionPreference);
  window.addEventListener('resize', resize, { passive: true });

  if (!preference.matches && !hasLoginError) {
    introStart = performance.now();
    lastDraw = introStart;
  }
  resize();
  if (introStart !== null) {
    page.classList.add('intro');
    // Reveal the form even if canvas animation stalls after the initial frame.
    setTimeout(() => page.classList.replace('intro', 'entered'), (windEnd / introSpeed + riseEnd) * 1000 + 250);
  }
  queue();
})();

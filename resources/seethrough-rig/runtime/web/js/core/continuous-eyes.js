// Opt-in semantic eyelid topology. Legacy model geometry/opacity is untouched.
// Contours run along the upper lid left->right, then lower lid right->left.
// All keys share X coordinates; only corresponding Y coordinates interpolate.
// This makes triangle area affine in the key weights, so validation at every
// endpoint proves no fold for any convex intermediate blink/expression mix.
const SHAPES = ['open', 'half', 'closed', 'jitome', 'happy', 'surprised', 'worried', 'shy'];
export const EYE_ART_KEYS = Object.freeze([...SHAPES]);
const EXPRESSIONS = [['jitome', 'EyeJitome'], ['happy', 'EyeHappy'], ['surprised', 'EyeSurprised'], ['worried', 'EyeWorried'], ['shy', 'EyeShy']];
const unit = v => Math.max(0, Math.min(1, Number.isFinite(v) ? v : 0));
const fail = message => { throw new Error('Invalid continuous eye: ' + message); };
const near = (a, b) => Math.abs(a - b) <= 1e-4;
const area = (p, a, b, c) => (p[2*b]-p[2*a])*(p[2*c+1]-p[2*a+1])-(p[2*b+1]-p[2*a+1])*(p[2*c]-p[2*a]);

// With fixed uploaded X, each canonical triangle area is affine in the shared
// convex key weights. Endpoint positivity alone is insufficient: independently
// rounding interpolated Y to GL FLOAT can collapse a very thin intermediate.
// Bound those independent errors over the entire convex hull, not sampled poses.
function validateConvexRingClearance(positions, endpointY, triangles) {
  const count = positions.length/2, maxima = new Float64Array(count), errors = new Float64Array(count);
  const f32 = new Float32Array(1), bits = new Uint32Array(f32.buffer);
  const epsilonAllowance = 128*Number.EPSILON;
  for (let i=0;i<count;i++) {
    for (const y of endpointY) maxima[i] = Math.max(maxima[i],Math.abs(y[i]));
    // The conservative double allowance covers convex weight normalization and
    // all five expression additions as well as interpolation arithmetic.
    const doubleError = epsilonAllowance*Math.max(1,maxima[i]);
    f32[0] = maxima[i]+doubleError;
    const exponent = (bits[0]>>>23)&255;
    const ulp = 2**(exponent===0 ? -149 : exponent-150);
    errors[i] = ulp+doubleError; // full ULP, deliberately larger than half-ULP
  }
  for (const [a,b,c] of triangles) {
    const ids = [a,b,c], x = ids.map(i=>positions[2*i]);
    // Coefficients use the same positive orientation as area() above.
    const coefficients = [x[2]-x[1],x[0]-x[2],x[1]-x[0]];
    let bound = 0;
    for (let i=0;i<3;i++) {
      bound += Math.abs(coefficients[i])*errors[ids[i]];
      // Include coefficient and area double-arithmetic error, even for large
      // translated coordinates with heavy cancellation in the signed area.
      bound += epsilonAllowance*(Math.abs(x[(i+1)%3])+Math.abs(x[(i+2)%3]))*Math.max(1,maxima[ids[i]]);
    }
    for (const y of endpointY) {
      const signed = coefficients.reduce((sum,value,i)=>sum+value*y[ids[i]],0);
      if (!(signed>bound && signed>1e-9)) fail('insufficient Float32 convex ring clearance');
    }
  }
}

export function validateContinuousEyes(model) {
  const eyes = model.rig?.eyes || {};
  const enabled = Object.entries(eyes).filter(([, eye]) => eye.continuous !== undefined);
  if (!enabled.length) {
    const referenced = value => {
      if (!value || typeof value !== 'object') return false;
      if (value.param === 'EyeJitome' || value.oneMinusSum?.includes('EyeJitome') || value.sum?.includes('EyeJitome')) return true;
      return Object.values(value).some(referenced);
    };
    const keyformReference = value => typeof value==='string' ? value.split(',').some(axis=>axis.split(':')[0]==='EyeJitome') : value&&typeof value==='object' ? Object.entries(value).some(([key,v])=>keyformReference(key)||keyformReference(v)) : false;
    if (referenced(model.drawables) || [...model.drawables,...(model.chains||[])].some(d=>keyformReference(d.keyforms||{})) || keyformReference(model.rig.keyforms) || (model.expressions||[]).some(e=>e.params?.EyeJitome)) fail('EyeJitome requires continuous opt-in');
  }
  if (enabled.length && (model.rig.head.mode !== 'rigid' || model.rig.body.rigid !== true)) fail('requires rigid face/body transforms');
  for (const [side, eye] of enabled) {
    const def = eye.continuous;
    if (!['L', 'R'].includes(side) || ![1,2,3].includes(def?.version) || !def.contours) fail('unsupported version/side');
    const fields = ['version','contours','halfOpen','browJitomeDrop','sources'];
    if (def.version === 3) fields.push('underLashCoverage', 'strokeGuides');
    if (Object.keys(def).some(key=>!fields.includes(key))) fail('unsupported definition field');
    if (def.version === 3 && def.underLashCoverage === undefined && def.strokeGuides === undefined) fail('v3 requires underLashCoverage or strokeGuides');
    if (def.underLashCoverage !== undefined) {
      const coverage = def.underLashCoverage;
      if (!coverage || typeof coverage !== 'object' || Array.isArray(coverage) || Object.keys(coverage).join(',') !== 'marginPx' || !Number.isFinite(coverage.marginPx) || coverage.marginPx <= 0 || coverage.marginPx > 2) fail('underLashCoverage requires only marginPx inside (0,2] source pixels');
    }
    const keys = def.contours, rest = keys.open;
    if (!Array.isArray(rest) || rest.length < 8 || rest.length > 512 || rest.length % 2) fail('even point count must be 8..512');
    const n = rest.length, half = n / 2;
    for (const name of ['open', 'half', 'closed', 'jitome', 'happy']) if (!keys[name]) fail('missing ' + name + ' contour');
    for (const [name, points] of Object.entries(keys)) {
      if (!SHAPES.includes(name) || !Array.isArray(points) || points.length !== n) fail('contour correspondence');
      for (let i = 0; i < n; i++) {
        if (!Array.isArray(points[i]) || points[i].length !== 2 || !points[i].every(value=>Number.isFinite(value)&&Math.abs(value)<=1e9) || points[i][0] !== rest[i]?.[0]) fail('fixed semantic X correspondence');
      }
      for (let i = 1; i <= half; i++) if (!(points[i][0] > points[i-1][0])) fail('upper lid X must increase');
      for (let i = 1; i < half; i++) {
        if (points[n-i][0] !== points[i][0] || points[i][1] > points[n-i][1] + 1e-5) fail('crossed upper/lower lids');
        if (def.version === 3 && (points[i][1]>points[n-i][1] || Math.fround(points[i][1])>Math.fround(points[n-i][1]))) fail('crossed Float32 upper/lower lids');
        if (name === 'open' && points[n-i][1] - points[i][1] <= 1e-6) fail('collapsed open contour');
        if (name === 'closed' && Math.abs(points[i][1]-points[n-i][1])>1e-6) fail('closed lid has an aperture');
        if (def.version === 3 && name === 'closed' && Math.fround(points[i][1])!==Math.fround(points[n-i][1])) fail('closed Float32 lid has an aperture');
      }
    }
    if (def.strokeGuides !== undefined) {
      const guides = def.strokeGuides;
      if (!guides || typeof guides !== 'object' || Array.isArray(guides) || Object.keys(guides).sort().join(',') !== Object.keys(keys).sort().join(',')) fail('stroke guides must cover every contour key');
      const corners = n+2;
      for (const [name, points] of Object.entries(guides)) {
        if (!Array.isArray(points) || points.length !== n+6) fail('stroke guide correspondence');
        for (let i=0;i<points.length;i++) {
          if (!Array.isArray(points[i]) || points[i].length !== 2 || !points[i].every(value=>Number.isFinite(value)&&Math.abs(value)<=1e9) || points[i][0] !== guides.open?.[i]?.[0]) fail('fixed stroke guide X correspondence');
        }
        for (let i=0;i<=half;i++) {
          const lower = i===0 || i===half ? i : n-i;
          if (points[i][0] !== rest[i][0] || points[half+1+i][0] !== rest[i][0] || !(points[i][1] < keys[name][i][1] && points[half+1+i][1] > keys[name][lower][1])) fail('stroke guides must surround the aperture');
        }
        const [tl,tr,br,bl] = points.slice(corners);
        if (!(tl[0] < rest[0][0] && tr[0] > rest[half][0] && tl[0]===bl[0] && tr[0]===br[0] && tl[1]<keys[name][0][1] && bl[1]>keys[name][0][1] && tr[1]<keys[name][half][1] && br[1]>keys[name][half][1])) fail('stroke guide wing bounds');
      }
    }
    if (def.halfOpen !== undefined && (!Number.isFinite(def.halfOpen) || def.halfOpen <= 0 || def.halfOpen >= 1)) fail('halfOpen must lie inside 0..1');
    if (def.browJitomeDrop !== undefined && (!Number.isFinite(def.browJitomeDrop) || Math.abs(def.browJitomeDrop) > model.canvas.height)) fail('brow drop');
    if (def.sources !== undefined && (!def.sources || Array.isArray(def.sources) || typeof def.sources !== 'object' || Object.entries(def.sources).some(([key,value])=>!(key in keys)||typeof value!=='string'||!value.length||value.length>120))) fail('invalid contour sources');
  }
  const sideBreath = new Map();
  for (const d of model.drawables) {
    const binding = d.eyeRig;
    if (!binding) continue;
    if (typeof binding !== 'object' || Array.isArray(binding) || typeof binding.side !== 'string' || Object.keys(binding).some(key=>!['side','role','indices','weights','artKeys'].includes(key))) fail('unsupported binding field/side');
    const def = eyes[binding.side]?.continuous;
    if (!def || !['ring', 'mask', 'iris', 'white'].includes(binding.role)) fail('drawable binding');
    if (binding.role !== 'mask' && d.isMask) fail('visible eye art cannot be mask-only');
    if (d.strandIds?.length || Object.keys(d.keyforms || {}).length || d.sub?.length || d.morph || (d.deformers || []).some(df => df.type !== 'iris' || binding.role !== 'iris' || df.eye !== binding.side)) fail('incompatible local/physics deformer');
    const n = d.positions.length / 2;
    if (n > 20000) fail('mesh vertex budget');
    if (def.version >= 2 && binding.role === 'ring') {
      const art=binding.artKeys, names=Object.keys(def.contours).sort();
      if(!art || typeof art!=='object' || Array.isArray(art) || Object.keys(art).sort().join(',')!==names.join(','))fail('registered art must cover every contour key');
      for(const [key,entry] of Object.entries(art)) {
        if(!entry || Object.keys(entry).sort().join(',')!=='atlas,uvs' || !Number.isInteger(entry.atlas) || entry.atlas<0 || entry.atlas>=model.atlases.length || !(entry.uvs instanceof Float32Array) || entry.uvs.length!==2*n || [...entry.uvs].some(v=>!Number.isFinite(v)||v < -1e-5 || v > 1.00001))fail('invalid registered art atlas/UV');
      }
      if(art.open.atlas!==d.atlas || art.open.uvs.some((v,i)=>v!==d.uvs[i]))fail('open art must match primary atlas/UV');
    } else if(binding.artKeys!==undefined)fail('registered art requires a v2/v3 ring');
    if (!sideBreath.has(binding.side)) sideBreath.set(binding.side,d.attrs.breath[0]);
    if ([...d.attrs.head].some(w=>w!==1) || [...d.attrs.breath].some(w=>w!==sideBreath.get(binding.side))) fail('eye art requires shared rigid head/breath attachment');
    if (['ring', 'mask'].includes(binding.role)) {
      const count = def.contours.open.length;
      const guided = binding.role === 'ring' && def.strokeGuides !== undefined;
      const controlCount = count + (guided ? count+6 : 0);
      const matches = def.version === 3 ? (a,b) => a === Math.fround(b) : near;
      let canonicalTriangles, ringEndpointY;
      if (!Array.isArray(binding.indices) || !Array.isArray(binding.weights) || binding.indices.length !== n || binding.weights.length !== n) fail('binding array length');
      for (let i = 0; i < n; i++) {
        const idx = binding.indices[i], weight = binding.weights[i];
        if (!Number.isInteger(idx) || idx < 0 || idx >= controlCount || !Number.isFinite(weight) || weight < 0 || weight > 1) fail('control index/weight');
        const rest = idx<count ? def.contours.open[idx] : def.strokeGuides.open[idx-count];
        if ((binding.role === 'mask' || weight === 1) && (weight !== 1 || !matches(d.positions[2*i], rest[0]) || !matches(d.positions[2*i+1], rest[1]))) fail('inner/mask/guide vertex must follow exact control');
      }
      if (binding.role === 'ring') {
        if (d.clip?.length) fail('continuous ring cannot be clipped');
        const mid=count/2, rest=def.contours.open;
        const lower=[0,...Array.from({length:mid-1},(_,i)=>count-1-i),mid];
        const controls=guided
          ? [...Array.from({length:2*count+6},(_,i)=>i),...Array.from({length:count+6},(_,i)=>count+i)]
          : [...Array.from({length:count},(_,i)=>i),...Array.from({length:mid+1},(_,i)=>i),...lower,0,mid,mid,0];
        if(n!==controls.length || binding.indices.some((v,i)=>v!==controls[i]) || binding.weights.some((w,i)=>w!==(i<(guided?2*count+6:count)?1:0))) fail('ring requires complete canonical rectangular annulus');
        const top=Array.from({length:mid+1},(_,i)=>count+i),bottom=Array.from({length:mid+1},(_,i)=>count+mid+1+i);
        const [tl,tr,br,bl]=[0,1,2,3].map(i=>2*count+2+i),wanted=[];
        for(let i=0;i<mid;i++)wanted.push([top[i],top[i+1],i+1],[top[i],i+1,i],[lower[i],lower[i+1],bottom[i+1]],[lower[i],bottom[i+1],bottom[i]]);
        wanted.push([0,tl,top[0]],[0,bl,tl],[0,bottom[0],bl],[mid,br,bottom.at(-1)],[mid,tr,br],[mid,top.at(-1),tr]);
        const fixedOffset = guided ? count+6 : 0;
        if (guided) {
          const loop = [...Array.from({length:mid+1},(_,i)=>i),count+3,count+4,...Array.from({length:mid+1},(_,i)=>count+1-i),count+5,count+2];
          for (let i=0;i<loop.length;i++) {
            const a=count+loop[i],b=count+loop[(i+1)%loop.length];
            wanted.push([a,a+fixedOffset,b+fixedOffset],[a,b+fixedOffset,b]);
          }
        }
        // Keep the geometric orientation of the canonical layout. Input index
        // winding may differ, but cannot legitimize an overlapping rest shell.
        canonicalTriangles = wanted;
        const key = ids=>[...ids].sort((a,b)=>a-b).join(','),actual=[];
        for(let i=0;i<d.indices.length;i+=3)actual.push(key([...d.indices.subarray(i,i+3)]));
        if(actual.sort().join(';')!==wanted.map(key).sort().join(';'))fail('incomplete or incorrect ring triangles');
        const p=d.positions,y0=p[2*(top[0]+fixedOffset)+1],y1=p[2*(bottom[0]+fixedOffset)+1],x0=p[2*(tl+fixedOffset)],x1=p[2*(tr+fixedOffset)];
        if(!(x0<rest[0][0]&&x1>rest[mid][0]&&y0<y1))fail('fixed ring bounds');
        for(let i=0;i<=mid;i++)if(!matches(p[2*(top[i]+fixedOffset)],rest[i][0])||!matches(p[2*(bottom[i]+fixedOffset)],rest[i][0])||!matches(p[2*(top[i]+fixedOffset)+1],y0)||!matches(p[2*(bottom[i]+fixedOffset)+1],y1))fail('fixed ring rows');
        for(const [i,x,y] of [[tl,x0,y0],[tr,x1,y0],[br,x1,y1],[bl,x0,y1]])if(!matches(p[2*(i+fixedOffset)],x)||!matches(p[2*(i+fixedOffset)+1],y))fail('fixed ring corners');
        if(guided)for(const points of Object.values(def.strokeGuides))for(const [x,y] of points)if(!(x>x0&&x<x1&&y>y0&&y<y1))fail('fixed ring must enclose every stroke guide');
        if (def.version === 3) {
          ringEndpointY = Object.entries(def.contours).map(([name,points]) => Float64Array.from({length:n},(_,i) => {
            const j = binding.indices[i];
            return binding.weights[i]===0 ? d.positions[2*i+1] : j<count ? points[j][1] : def.strokeGuides[name][j-count][1];
          }));
        }
      }
      if (binding.role === 'mask') {
        if (d.isMask !== true || d.clip?.length || n !== def.contours.open.length || new Set(binding.indices).size !== n) fail('incomplete contour mask');
        const mid = n / 2, lower = [0,...Array.from({length:mid-1},(_,i)=>n-1-i),mid];
        const wanted = [], actual = [];
        const triangleKey = ids => ids.sort((a,b)=>a-b).join(',');
        for (let i=0;i<mid;i++) {
          if (i) wanted.push(triangleKey([i,lower[i+1],lower[i]]));
          if (i<mid-1) wanted.push(triangleKey([i,i+1,lower[i+1]]));
        }
        for (let i=0;i<d.indices.length;i+=3) actual.push(triangleKey([...d.indices.subarray(i,i+3)].map(j=>binding.indices[j])));
        if (actual.sort().join(';') !== wanted.sort().join(';')) fail('mask must use semantic aperture strip');
        // Canonical strip winding is positive in this model-space convention.
        // Use semantic indices rather than caller-supplied triangle winding.
        if (def.version === 3) {
          for (const points of Object.values(def.contours)) {
            const p=Float32Array.from(points.flat());
            for(let i=0;i<mid;i++) {
              if ((i && area(p,i,lower[i+1],lower[i])<0) || (i<mid-1 && area(p,i,i+1,lower[i+1])<0)) fail('crossed Float32 mask triangle');
            }
          }
        }
      }
      // Source positions are float32. Use a relative tolerance for rounding,
      // but reject genuine inverted/collapsed ring triangles at any key.
      for (const [name,points] of Object.entries(def.contours)) {
        // New models must prove the actual GL FLOAT endpoint geometry, rather
        // than relying on gaps that disappear when uploaded to the renderer.
        const posed = def.version === 3 ? Float32Array.from(d.positions) : Float64Array.from(d.positions);
        for (let i = 0; i < n; i++) {
          const j = binding.indices[i], w = binding.weights[i];
          const point = j<count ? points[j] : def.strokeGuides[name][j-count];
          const rest = j<count ? def.contours.open[j] : def.strokeGuides.open[j-count];
          if (w === 1) { posed[2*i] = point[0]; posed[2*i+1] = point[1]; }
          else posed[2*i+1] += w * (point[1] - rest[1]);
        }
        if (def.version === 3 && canonicalTriangles) {
          for (const [a,b,c] of canonicalTriangles) {
            if (!(area(d.positions,a,b,c)>1e-9 && area(posed,a,b,c)>1e-9)) fail('overlapping/collapsed canonical ring topology');
          }
        }
        for (let i = 0; i < d.indices.length; i += 3) {
          const [a,b,c] = d.indices.subarray(i,i+3), before = area(d.positions,a,b,c), after = area(posed,a,b,c);
          if (!Number.isFinite(before) || !Number.isFinite(after) || Math.abs(before) < 1e-9) fail('degenerate/nonfinite rest triangle');
          const relative = after / before;
          if (relative < -1e-6 || (binding.role === 'ring' && relative <= 1e-8)) fail('flipped/collapsed ring or mask triangle');
        }
      }
      if (ringEndpointY) validateConvexRingClearance(d.positions,ringEndpointY,canonicalTriangles);
    } else {
      if (binding.indices !== undefined || binding.weights !== undefined) fail('rigid art cannot use contour weights');
      const masks = (d.clip || []).map(id => model.drawables.find(other => other.id === id));
      if (masks.length !== 1 || masks[0]?.eyeRig?.role !== 'mask' || masks[0]?.eyeRig?.side !== binding.side) fail('eye art requires its shared continuous mask');
    }
  }
  for (const [side] of enabled) {
    const bindings = model.drawables.filter(d => d.eyeRig?.side === side);
    if (bindings.filter(d => d.eyeRig.role === 'mask').length !== 1 || !['ring','iris','white'].every(role=>bindings.some(d=>d.eyeRig.role===role))) fail('missing or duplicate mask/ring/iris/white');
  }
  const registeredRings=new Set(model.drawables.filter(d=>d.eyeRig?.artKeys).map(d=>d.id));
  for(const d of model.drawables)for(const id of d.clip||[])if(registeredRings.has(id))fail('registered ring cannot be used as a clipping mask');
}

export function expressionWeights(def, P) {
  const weights = EXPRESSIONS.filter(([key]) => def.contours[key]).map(([key, param]) => [key, unit(P[param])]);
  const sum = weights.reduce((total, [, value]) => total + value, 0);
  return { weights: weights.map(([key, value]) => [key, value / Math.max(1, sum)]), neutral: Math.max(0, 1-sum) };
}

export function evaluateContinuousEye(def, P, side) {
  const {open} = def.contours;
  const openness = unit(P[side === 'L' ? 'EyeLOpen' : 'EyeROpen'] ?? 1);
  const split = def.halfOpen ?? 0.5;
  const t = openness < split ? openness/split : (openness-split)/(1-split);
  const {weights,neutral} = expressionWeights(def,P);
  const artWeights=new Float32Array(SHAPES.length);
  const aKey=openness<split?'closed':'half',bKey=openness<split?'half':'open';
  artWeights[SHAPES.indexOf(aKey)]+=neutral*(1-t);
  artWeights[SHAPES.indexOf(bKey)]+=neutral*t;
  for(const [key,w]of weights){artWeights[SHAPES.indexOf(key)]+=w*openness;artWeights[2]+=w*(1-openness);}
  const evaluatePoints = keys => {
    const points = new Float64Array(keys.open.length*2);
    for (let i=0;i<keys.open.length;i++) {
      points[2*i]=keys.open[i][0];
      let y=(keys[aKey][i][1]+(keys[bKey][i][1]-keys[aKey][i][1])*t)*neutral;
      for (const [name,w] of weights) y+=(keys.closed[i][1]+(keys[name][i][1]-keys.closed[i][1])*openness)*w;
      points[2*i+1]=y;
    }
    return points;
  };
  const points = evaluatePoints(def.contours);
  let gap=0;
  for (let i=1;i<open.length/2;i++) gap=Math.max(gap,points[2*(open.length-i)+1]-points[2*i+1]);
  const evaluated = {points,closed:openness===0||gap<=1e-5,jitome:weights.find(([key])=>key==='jitome')?.[1]??0,artWeights};
  if (def.strokeGuides) evaluated.strokeGuides = evaluatePoints(def.strokeGuides);
  return evaluated;
}

// A separate, source-pixel coverage envelope, never a replacement aperture or
// an art-authoring operation. Runtime stencil support must additionally test the
// current registered lash alpha. Evaluating before head/body/camera transforms
// makes the bounded margin independent of DPR, zoom and render target size.
export function evaluateUnderLashCoverage(def, evaluated) {
  const points = new Float64Array(evaluated.points);
  if (!def.underLashCoverage || evaluated.closed) return points;
  const n = points.length / 2, mid = n / 2;
  const x0 = points[0], span = points[2*mid] - x0;
  for (let i=1;i<mid;i++) {
    const lower = n-i;
    const gap = Math.max(0, points[2*lower+1] - points[2*i+1]);
    const t = (points[2*i]-x0)/span;
    const margin = def.underLashCoverage.marginPx * Math.sqrt(Math.max(0, Math.sin(Math.PI*t))) * Math.min(1, gap/4);
    points[2*i+1] -= margin;
    points[2*lower+1] += margin;
  }
  // The shared corner vertices are intentionally untouched, even at t=1 where
  // floating-point sin(pi) is not exactly zero.
  return points;
}

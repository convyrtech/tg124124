// QR decoder using jsQR - optimized for Telegram rounded QR codes
const jsQR = require('jsqr');
const { Jimp } = require('jimp');

async function decodeQR(imagePath) {
    try {
        const image = await Jimp.read(imagePath);
        const width = image.bitmap.width;
        const height = image.bitmap.height;

        // Helper: convert to grayscale manually
        function toGrayscale(img) {
            const clone = img.clone();
            clone.scan(0, 0, clone.bitmap.width, clone.bitmap.height, function(x, y, idx) {
                const r = this.bitmap.data[idx];
                const g = this.bitmap.data[idx + 1];
                const b = this.bitmap.data[idx + 2];
                const gray = Math.round(0.299 * r + 0.587 * g + 0.114 * b);
                this.bitmap.data[idx] = gray;
                this.bitmap.data[idx + 1] = gray;
                this.bitmap.data[idx + 2] = gray;
            });
            return clone;
        }

        // Helper: invert colors
        function invert(img) {
            const clone = img.clone();
            clone.scan(0, 0, clone.bitmap.width, clone.bitmap.height, function(x, y, idx) {
                this.bitmap.data[idx] = 255 - this.bitmap.data[idx];
                this.bitmap.data[idx + 1] = 255 - this.bitmap.data[idx + 1];
                this.bitmap.data[idx + 2] = 255 - this.bitmap.data[idx + 2];
            });
            return clone;
        }

        // Helper: threshold
        function threshold(img, thresh = 128) {
            const clone = toGrayscale(img);
            clone.scan(0, 0, clone.bitmap.width, clone.bitmap.height, function(x, y, idx) {
                const val = this.bitmap.data[idx] > thresh ? 255 : 0;
                this.bitmap.data[idx] = val;
                this.bitmap.data[idx + 1] = val;
                this.bitmap.data[idx + 2] = val;
            });
            return clone;
        }

        // Try multiple processing variants
        const variants = [
            { name: 'original', img: image },
            { name: 'inverted', img: invert(image) },
            { name: 'grayscale', img: toGrayscale(image) },
            { name: 'gray_inverted', img: invert(toGrayscale(image)) },
            { name: 'threshold', img: threshold(image, 128) },
            { name: 'threshold_inv', img: invert(threshold(image, 128)) },
            { name: 'threshold_low', img: threshold(image, 100) },
            { name: 'threshold_high', img: threshold(image, 160) },
        ];

        for (const { name, img } of variants) {
            const imageData = new Uint8ClampedArray(img.bitmap.data);

            const code = jsQR(imageData, img.bitmap.width, img.bitmap.height, {
                inversionAttempts: 'attemptBoth'
            });

            if (code && code.data && code.data.includes('tg://login')) {
                console.log(code.data);
                return;
            }
        }

        console.log('QR_NOT_FOUND');
    } catch (err) {
        console.error('ERROR:', err.message);
        process.exit(1);
    }
}

const imagePath = process.argv[2];
if (!imagePath) {
    console.error('Usage: node decode_qr.js <image_path>');
    process.exit(1);
}

decodeQR(imagePath);

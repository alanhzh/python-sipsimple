/*
  Copyright (C) 2012 Werner Dittmann

  This library is free software; you can redistribute it and/or
  modify it under the terms of the GNU Lesser General Public
  License as published by the Free Software Foundation; either
  version 2.1 of the License, or (at your option) any later version.

  This library is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
  Lesser General Public License for more details.

  You should have received a copy of the GNU Lesser General Public
  License along with this library; if not, write to the Free Software
  Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

 * In addition, as a special exception, the copyright holders give
 * permission to link the code of portions of this program with the
 * OpenSSL library under certain conditions as described in each
 * individual source file, and distribute linked combinations
 * including the two.
 * You must obey the GNU General Public License in all respects
 * for all of the code used other than OpenSSL.  If you modify
 * file(s) with this exception, you may extend this exception to your
 * version of the file(s), but you are not obligated to do so.  If you
 * do not wish to do so, delete this exception statement from your
 * version.  If you delete this exception statement from all source
 * files in the program, then also delete it here.
 */

/*
 * Authors: Werner Dittmann
 */

#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include "zrtp/crypto/sha2.h"
#include "zrtp/crypto/hmac384.h"

typedef struct _hmacSha384Context {
    sha384_ctx ctx;
    sha384_ctx innerCtx;
    sha384_ctx outerCtx;
} hmacSha384Context;

static int32_t hmacSha384Init(hmacSha384Context *ctx, const uint8_t *key, uint32_t kLength)
{
    int32_t i;
    uint8_t localPad[SHA384_BLOCK_SIZE] = {0};
    uint8_t localKey[SHA384_BLOCK_SIZE] = {0};

    if (key == NULL)
        return 0;

    memset(ctx, 0, sizeof(hmacSha384Context));

    /* check key length and reduce it if necessary */
    if (kLength > SHA384_BLOCK_SIZE) {
        sha384_begin(&ctx->ctx);
        sha384_hash(key, kLength, &ctx->ctx);
        sha384_end(localKey, &ctx->ctx);
    }
    else {
        memcpy(localKey, key, kLength);
    }
    /* prepare inner hash and hold the context */
    for (i = 0; i < SHA384_BLOCK_SIZE; i++)
        localPad[i] = localKey[i] ^ 0x36;

    sha384_begin(&ctx->innerCtx);
    sha384_hash(localPad, SHA384_BLOCK_SIZE, &ctx->innerCtx);

    /* prepare outer hash and hold the context */
    for (i = 0; i < SHA384_BLOCK_SIZE; i++)
        localPad[i] = localKey[i] ^ 0x5c;

    sha384_begin(&ctx->outerCtx);
    sha384_hash(localPad, SHA384_BLOCK_SIZE, &ctx->outerCtx);

    /* copy prepared inner hash to work hash - ready to process data */
    memcpy(&ctx->ctx, &ctx->innerCtx, sizeof(sha384_ctx));

    memset(localKey, 0, sizeof(localKey));

    return 1;
}

static void hmacSha384Reset(hmacSha384Context *ctx)
{
    /* copy prepared inner hash to work hash context */
    memcpy(&ctx->ctx, &ctx->innerCtx, sizeof(sha384_ctx));
}

static void hmacSha384Update(hmacSha384Context *ctx, const uint8_t *data, uint32_t dLength)
{
    /* hash new data to work hash context */
    sha384_hash(data, dLength, &ctx->ctx);
}

static void hmacSha384Final(hmacSha384Context *ctx, uint8_t *mac)
{
    uint8_t tmpDigest[SHA384_DIGEST_SIZE];

    /* finalize work hash context */
    sha384_end(tmpDigest, &ctx->ctx);

    /* copy prepared outer hash to work hash */
    memcpy(&ctx->ctx, &ctx->outerCtx, sizeof(sha384_ctx));

    /* hash inner digest to work (outer) hash context */
    sha384_hash(tmpDigest, SHA384_DIGEST_SIZE, &ctx->ctx);

    /* finalize work hash context to get the hmac*/
    sha384_end(mac, &ctx->ctx);
}


void hmac_sha384(uint8_t *key, uint32_t keyLength, uint8_t* data, int32_t dataLength, uint8_t* mac, uint32_t* macLength)
{
    hmacSha384Context ctx;

    hmacSha384Init(&ctx, key, keyLength);
    hmacSha384Update(&ctx, data, dataLength);
    hmacSha384Final(&ctx, mac);
    *macLength = SHA384_DIGEST_SIZE;
}

void hmac_sha384( uint8_t* key, uint32_t keyLength, uint8_t* dataChunks[], uint32_t dataChunckLength[],
                uint8_t* mac, uint32_t* macLength )
{
    hmacSha384Context ctx;

    hmacSha384Init(&ctx, key, keyLength);

    while (*dataChunks) {
        hmacSha384Update(&ctx, *dataChunks, *dataChunckLength);
        dataChunks ++;
        dataChunckLength ++;
    }
    hmacSha384Final(&ctx, mac);
    *macLength = SHA384_DIGEST_SIZE;
}

void* createSha384HmacContext(uint8_t* key, int32_t keyLength)
{
    hmacSha384Context *ctx = reinterpret_cast<hmacSha384Context*>(malloc(sizeof(hmacSha384Context)));

    if (ctx != NULL) {
        hmacSha384Init(ctx, key, keyLength);
    }
    return ctx;
}

void hmacSha384Ctx(void* ctx, const uint8_t* data, uint32_t dataLength,
                uint8_t* mac, int32_t* macLength)
{
    hmacSha384Context *pctx = (hmacSha384Context*)ctx;

    hmacSha384Reset(pctx);
    hmacSha384Update(pctx, data, dataLength);
    hmacSha384Final(pctx, mac);
    *macLength = SHA384_DIGEST_SIZE;
}

void hmacSha384Ctx(void* ctx, const uint8_t* data[], uint32_t dataLength[],
                uint8_t* mac, int32_t* macLength )
{
    hmacSha384Context *pctx = (hmacSha384Context*)ctx;

    hmacSha384Reset(pctx);
    while (*data) {
        hmacSha384Update(pctx, *data, *dataLength);
        data++;
        dataLength++;
    }
    hmacSha384Final(pctx, mac);
    *macLength = SHA384_DIGEST_SIZE;
}

void freeSha384HmacContext(void* ctx)
{
    if (ctx) {
        memset(ctx, 0, sizeof(hmacSha384Context));
        free(ctx);
    }
}